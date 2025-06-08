import os
import logging
import pickle
from collections import Counter, defaultdict

import wandb
import networkx as nx
from tqdm import tqdm
import numpy as np
import pandas as pd
import torch

from snne.kle.core import vn_entropy, normalize_kernel
from snne.kle.kernels import heat_kernel, matern_kernel
from snne.uncertainty.utils.eval_utils import auroc, auarc, aucpr, is_binary_list
from snne.uncertainty.utils import utils
from snne.uncertainty.uncertainty_measures.semantic_entropy import logsumexp_by_id
from snne.uncertainty.utils.compute_utils import get_parser, setup_wandb
from snne.uncertainty.uncertainty_measures.kernel_uncertainty import get_entailment_graph, get_semantic_ids_graph, EntailmentDeberta


# KLE hyperparameters
ALPHAS_RANGE = np.arange(0, 1.01, 0.1)
HEAT_T_RANGE = np.arange(0.1, 0.71, 0.1)
MATERN_KAPPA_RANGE = [1.0, 2.0, 3.0]
MATERN_NU_RANGE = [1.0, 2.0, 3.0]


def get_from_sem_to_sentence_id(ordered_ids):
    from_sem_to_sentence_id = defaultdict(list)
    for i, el in enumerate(ordered_ids):
        from_sem_to_sentence_id[el].append(i)
    return from_sem_to_sentence_id


def reorder_by_semantic_ids(graph, semantic_ids, ordered_sem_ids):
    from_sem_to_sentence_id = get_from_sem_to_sentence_id(semantic_ids)
    new_graph = nx.Graph()
    for sem_id in ordered_sem_ids:
        for sent_id in from_sem_to_sentence_id[sem_id]:
            new_graph.add_node(sent_id)
    
    new_graph.add_edges_from(graph.edges)
    return new_graph


def get_kernels(graph):
    kernels = {}
    for t in HEAT_T_RANGE:
        kernels[f"heat_t={t:.2}"] = heat_kernel(graph, t=t)
        kernels[f"heatn_t={t:.2}"] = heat_kernel(graph, t=t, norm_lapl=True)

    for kappa in MATERN_KAPPA_RANGE:
        for nu in MATERN_NU_RANGE:
            kernels[f"matern_kappa={kappa:.2}_nu={nu:.2}"] = matern_kernel(graph, kappa=kappa, nu=nu)
            kernels[f"maternn_kappa={kappa:.2}_nu={nu:.2}"] = matern_kernel(graph, kappa=kappa, nu=nu, norm_lapl=True)

    return kernels


def all_graph_entropies(graph):
    kernels = get_kernels(graph)
    results = []
    for kernel_name, kernel in kernels.items():
        for scale in [True, False]:
            kernel_entropy = vn_entropy(kernel, scale=scale)
            postfix = "_s" if scale else ""
            results.append((f'{kernel_name}_kernel_entropy{postfix}', kernel_entropy))
    return results


def get_block_diagonal_sem_kernel(log_likelihoods_per_sem_id, semantic_ids, ordered_sem_ids):
    from_sem_to_sentence_id = get_from_sem_to_sentence_id(semantic_ids)
    blocks = []
    for i, sem_id in enumerate(ordered_sem_ids):
        block_size = len(from_sem_to_sentence_id[sem_id])
        blocks.append(torch.exp(torch.tensor(log_likelihoods_per_sem_id[sem_id])) * torch.ones((block_size, block_size)) / block_size)
    return torch.block_diag(*blocks)  


def full_sem_unc_plus_klu(graph, log_likelihoods_per_sem_id, semantic_ids, ordered_sem_ids):
    graph = reorder_by_semantic_ids(graph, semantic_ids, ordered_sem_ids)
    block_diag_sem_kernel = get_block_diagonal_sem_kernel(
        log_likelihoods_per_sem_id=log_likelihoods_per_sem_id,
        semantic_ids=semantic_ids, ordered_sem_ids=ordered_sem_ids)

    alphas = ALPHAS_RANGE
    results = []
    kernels = get_kernels(graph)
    for kernel_name, kernel in kernels.items():
        for alpha in alphas:
            kernel = normalize_kernel(kernel) / kernel.shape[0]
            avg_kernel = alpha * torch.tensor(kernel) + (1 - alpha) * block_diag_sem_kernel
            avg_kernel = avg_kernel.numpy()
            success = False
            for jitter in [0, 1e-16, 1e-12]:
                try:
                    results.append(
                        (f"full_klu_{kernel_name}_alpha_{alpha:.2}", 
                        vn_entropy(
                            avg_kernel, normalize=False,
                            scale=False, jitter=jitter
                    )))
                    success = True
                    if jitter > 0:
                        logging.warn(f"Had to use jitter for numerical stability: {jitter}")
                    break
                except:
                    continue
            if not success:
                raise ValueError(f"Unable to calculate VNE for kernel {avg_kernel}")
    return results


def all_semantic_entropies(semantic_graph, log_likelihoods_per_sem_id):
    sem_entropies = torch.diag(torch.exp(torch.tensor(log_likelihoods_per_sem_id)))
    alphas = ALPHAS_RANGE
    results = []
    kernels = get_kernels(semantic_graph)
    for kernel_name, kernel in kernels.items():
        for alpha in alphas:
            kernel = normalize_kernel(kernel) / kernel.shape[0]
            avg_kernel = alpha * torch.tensor(kernel) + (1 - alpha) * sem_entropies
            avg_kernel = avg_kernel.numpy()
            results.append(
                (f"semantic_kernel_{kernel_name}_alpha_{alpha:.2}", 
                vn_entropy(avg_kernel, normalize=False, scale=False
            )))
    return results


def all_semantic_entropies_diag(semantic_graph, log_likelihoods_per_sem_id):
    sem_entropies = torch.exp(torch.tensor(log_likelihoods_per_sem_id))
    results = []
    kernels = get_kernels(semantic_graph)

    for kernel_name, kernel in kernels.items():
        kernel = normalize_kernel(kernel) / kernel.shape[0]
        kernel_prod = torch.tensor(kernel) * sem_entropies
        kernel_sum = torch.tensor(kernel) + sem_entropies
        results.append(
            (f"semantic_kernel_prod_{kernel_name}", 
            vn_entropy(kernel_prod, normalize=True, scale=False
        )))
        results.append(
            (f"semantic_kernel_sum_{kernel_name}", 
            vn_entropy(kernel_sum, normalize=True, scale=False
        )))
    return results


def compute_metrics(list_responses, list_num_generations, validation_is_true, list_generation_log_likelihoods, list_semantic_ids, entailment_model):
    validation_is_false = [1.0 - is_t for is_t in validation_is_true]
    is_binary = is_binary_list(validation_is_false)
    entropies = defaultdict(list)

    for idx in tqdm(range(len(validation_is_true))):
        responses = list_responses[idx]
        log_liks_agg = list_generation_log_likelihoods[idx][:list_num_generations[idx]]
        semantic_ids = list_semantic_ids[idx][:list_num_generations[idx]]
        unique_ids, log_likelihood_per_semantic_id = logsumexp_by_id(
            semantic_ids, 
            log_liks_agg, 
            agg='sum_normalized', 
            return_unique_ids=True
        )
        # Compute KLE
        graph = get_entailment_graph(
            responses, model=entailment_model,
            example=example, is_weighted=False
        )
        
        for k, value in all_graph_entropies(graph):
            entropies[k].append(value)
            
        weighted_graph = get_entailment_graph(
            responses, model=entailment_model,
            example=example, is_weighted=True
        )
        
        for k, value in all_graph_entropies(weighted_graph):
            entropies[f"weighted_{k}"].append(value)

        weighted_graph_deberta = get_entailment_graph(
            responses, model=entailment_model,
            example=example, is_weighted=True, weight_strategy="deberta"
        )
        for k, value in all_graph_entropies(weighted_graph_deberta):
            entropies[f"weighted_deberta_{k}"].append(value)

        semantic_graph = get_semantic_ids_graph(
            responses, semantic_ids=semantic_ids, ordered_ids=unique_ids, model=entailment_model,
            example=example
        )
        for k, value in all_semantic_entropies(semantic_graph, log_likelihood_per_semantic_id):
            entropies[k].append(value)
            
        for k, value in all_semantic_entropies_diag(semantic_graph, log_likelihood_per_semantic_id):
            entropies[k].append(value)
            
        for k, value in full_sem_unc_plus_klu(weighted_graph, log_likelihood_per_semantic_id, semantic_ids=semantic_ids, ordered_sem_ids=unique_ids):
            entropies[k].append(value)

        for k, value in full_sem_unc_plus_klu(weighted_graph_deberta, log_likelihood_per_semantic_id, semantic_ids=semantic_ids, ordered_sem_ids=unique_ids):
            entropies[f"deberta_{k}"].append(value)

    # Collect AUROC score
    list_auroc = []
    list_auarc = []
    list_aucpr = []
    list_method_name = []
    list_is_blackbox = []

    for entropy_name, entropy in entropies.items():
        if is_binary:
            _auroc = auroc(validation_is_false, entropy)
        else:
            _auroc = -1
        _auarc = auarc(entropy, validation_is_true)
        _aucpr = aucpr(entropy, validation_is_true)
        list_auroc.append(_auroc)
        list_auarc.append(_auarc)
        list_aucpr.append(_aucpr)
        list_method_name.append(entropy_name)
        if 'semantic' in entropy_name or 'full_klu' in entropy_name:
            list_is_blackbox.append(False)
        else:
            list_is_blackbox.append(True)

    data_metrics = {
        'method': list_method_name,
        'auroc': list_auroc,
        'auarc': list_auarc,
        'prr': list_aucpr,
        'is_blackbox': list_is_blackbox
    }

    df_metrics = pd.DataFrame(data_metrics)
    
    return df_metrics


# Set up log
utils.setup_logger()

# Parse arguments
args = get_parser()
logging.info("Args: %s", args)
utils.set_all_seeds(args.random_seed)

# Set up wandb
setup_wandb(args, prefix='compute_kle')

# Load pre-computed results
with open(f"{args.data_path}/validation_generations.pkl", 'rb') as infile:
    validation_generations = pickle.load(infile)
    
with open(f"{args.data_path}/uncertainty_measures.pkl", 'rb') as infile:
    results_old = pickle.load(infile)
    
# list_semantic_entropy = results_old['uncertainty_measures']['semantic_entropy']
list_semantic_ids = results_old['semantic_ids']

# Load models   
if args.entailment_model == 'deberta':
    entailment_model = EntailmentDeberta(args.entailment_cache_id, args.entailment_cache_only)
else:
    raise ValueError
logging.info('Entailment model loading complete.')

# Collect info
validation_is_true, validation_answerable = [], []
list_context, list_question, list_reference  = [], [], []
list_sum_token_log_likelihoods, list_avg_token_log_likelihoods = [], []
list_generation_log_likelihoods = []
list_responses = []

for idx, tid in tqdm(enumerate(validation_generations)):
    example = validation_generations[tid]
    question = example['question']
    full_responses = example["responses"][:args.num_generations]
    example_generation_log_likelihoods = []
    
    responses = [fr[0] for fr in full_responses]
    if args.condition_on_question and args.entailment_model == 'deberta':
        responses = [f'{question} {r}' for r in responses]
    list_responses.append(responses)
    
    for gen_info in full_responses:
        # Length normalization of generation probability
        example_generation_log_likelihoods.append(np.mean(gen_info[1]))
    
    most_likely_answer = example['most_likely_answer']
    is_true = most_likely_answer['accuracy']
    
    token_log_likelihoods = most_likely_answer['token_log_likelihoods']
    validation_is_true.append(is_true)
    validation_answerable.append(utils.is_answerable(example))
    list_generation_log_likelihoods.append(example_generation_log_likelihoods)
    
print(Counter(validation_answerable), Counter(validation_is_true))

# Calculate AUROC and AUARC
list_num_generations = [10 for _ in range(len(validation_is_true))]
print(sum(list_num_generations))
df_metrics = compute_metrics(
    list_responses,
    list_num_generations, 
    validation_is_true, 
    list_generation_log_likelihoods, 
    list_semantic_ids,
    entailment_model
)
logging.info(df_metrics.head())
os.makedirs('kle_results', exist_ok=True)
df_metrics.to_csv(f'kle_results/{args.dataset}_{args.model_name}_{args.num_generations}generations{args.suffix}_seed{args.random_seed}.csv', index=False)

wandb.finish()