"""Non-secret configuration and observed execution, separate from release approval."""
import hashlib
import json
import os
from uuid import uuid4

CONFIGURATION_KEYS = (
    "api_default_planner", "environment_default_planner", "evaluation_requested_planner",
    "relevance_enabled", "relevance_provider", "enabled_model_providers",
    "production_configuration_approved", "scope",
)

OPTIONAL_CONFIGURATION_KEYS = ("planner_requested_model", "relevance_requested_model",
                               "planner_reasoning_effort", "relevance_reasoning_effort",
                               "planner_max_output_tokens", "relevance_max_output_tokens", "candidate_mode",
                               "embedding_model", "embedding_model_sha256")

def content_digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def execution_identity(config, trajectories, queries, input_manifest_sha256, *, run_id=None):
    return {"schema_version": "execution-identity-v1", "run_id": run_id or str(uuid4()),
            "input_manifest_sha256": input_manifest_sha256, "configuration_sha256": content_digest(config),
            "planner_trajectories_sha256": content_digest(trajectories), "retrieval_queries_sha256": content_digest(queries)}


def configuration_snapshot(eval_provider=None):
    return {
        'candidate_mode': os.getenv('EVIDENCE_CANDIDATE_MODE', 'lexical'),
        'embedding_model': os.getenv('EVIDENCE_LOCAL_EMBEDDING_MODEL', ''),
        'embedding_model_sha256': os.getenv('EVIDENCE_LOCAL_EMBEDDING_SHA256', ''),
        'api_default_planner': 'deterministic',
        'environment_default_planner': os.getenv('INVESTIGATION_PLANNER_PROVIDER', 'deterministic'),
        'evaluation_requested_planner': eval_provider,
        'relevance_enabled': os.getenv('EVIDENCE_RELEVANCE_ENABLED', '0') == '1',
        'relevance_provider': os.getenv('EVIDENCE_RELEVANCE_PROVIDER', ''),
        'planner_requested_model': os.getenv('INVESTIGATION_MODEL', 'deepseek-flash'),
        'relevance_requested_model': os.getenv('EVIDENCE_RELEVANCE_MODEL', 'deepseek-flash'),
        'planner_reasoning_effort': os.getenv('INVESTIGATION_REASONING_EFFORT', 'low'),
        'relevance_reasoning_effort': os.getenv('EVIDENCE_RELEVANCE_REASONING_EFFORT', 'low'),
        'planner_max_output_tokens': int(os.getenv('INVESTIGATION_MAX_OUTPUT_TOKENS', '3000')),
        'relevance_max_output_tokens': int(os.getenv('EVIDENCE_RELEVANCE_MAX_OUTPUT_TOKENS', '3000')),
        'enabled_model_providers': sorted(value.strip().lower() for value in os.getenv('INSIGHT_ENABLED_MODEL_PROVIDERS', '').split(',') if value.strip()),
        'production_configuration_approved': False,
        'scope': 'API explicit request overrides API default; evaluation provider is independent of environment default; secrets excluded',
    }


def observed_provider_receipt(row, kind, config):
    if row.get('execution_origin') != 'provider_response':
        return False
    provenance = row.get('planner' if kind == 'planner' else 'provider')
    # Legacy frozen receipts retain their historical contract; new gateways require
    # request/response identifiers, without claiming an alias pins a weights version.
    legacy = 'deepseek-v4-flash-agent-planner-v2' if kind == 'planner' else 'deepseek-v4-flash-relevance-v1'
    current = 'deepseek-agent-planner-v3' if kind == 'planner' else 'deepseek-relevance-v2'
    if provenance == legacy:
        return True
    receipt = row.get('model_receipt') or {}
    return (provenance == current and receipt.get('provider') == 'deepseek'
            and (kind + '_reasoning_effort' not in config or receipt.get('reasoning_effort') == config[kind + '_reasoning_effort'])
            and (kind + '_max_output_tokens' not in config or receipt.get('max_output_tokens') == config[kind + '_max_output_tokens'])
            and receipt.get('requested_model') == config.get(kind + '_requested_model')
            and isinstance(receipt.get('reported_model'), str) and bool(receipt['reported_model'])
            and isinstance(receipt.get('provider_response_id'), str) and bool(receipt['provider_response_id']))


def bind_observed_execution(config, trajectories, retrieval_outputs):
    decisions = [decision for trace in trajectories for decision in trace.get('decisions', [])]
    observed = sorted({decision.get('planner', 'unknown') for decision in decisions})
    requested = config['evaluation_requested_planner']
    external = [decision for decision in decisions if observed_provider_receipt(decision, 'planner', config)]
    errors = []
    if requested == 'deepseek' and not external:
        errors.append('requested_model_without_observed_provider_execution')
    if requested == 'deterministic' and external:
        errors.append('unexpected_external_planner_execution')
    flags = {row['algorithm_manifest']['relevance_enabled'] for row in retrieval_outputs.values()}
    if flags != {config['relevance_enabled']}:
        errors.append('relevance_configuration_execution_mismatch')
    judgments = [row.get('relevance_judgment', {}) for row in retrieval_outputs.values()]
    providers = sorted({row.get('provider') for row in judgments if row.get('provider')})
    relevance_dispatches = sum(observed_provider_receipt(row, 'relevance', config) and row.get('dispatch_attempted') is True and not row.get('cache_reused') for row in judgments)
    relevance_responses = sum(observed_provider_receipt(row, 'relevance', config) and row.get('dispatch_attempted') is True and row.get('response_received') is True and not row.get('cache_reused') for row in judgments)
    if any(row.get('response_received') and not row.get('dispatch_attempted') for row in judgments):
        errors.append('relevance_response_without_dispatch')
    return {**config, 'external_relevance_dispatch_count': relevance_dispatches, 'external_relevance_response_count': relevance_responses, 'observed_planners': observed, 'observed_relevance_providers': providers,
            'external_planner_decision_count': len(external), 'real_provider_run': bool(external or relevance_responses),
            'configuration_execution_status': 'failed' if errors else 'matched', 'errors': errors,
            'quality_or_business_accepted': False}


def validate_external_planner_selection():
    from decimal import Decimal, InvalidOperation
    enabled = configuration_snapshot()['enabled_model_providers']
    if 'deepseek' not in enabled:
        raise ValueError('planner provider not enabled')
    for name in ['INVESTIGATION_APPROVAL_REFERENCE', 'INVESTIGATION_REGION']:
        if not os.getenv(name, '').strip():
            raise ValueError('planner region and transmission approval reference required')
    try:
        rates = [Decimal(os.getenv(name, '')) for name in ['INVESTIGATION_INPUT_COST_PER_MILLION', 'INVESTIGATION_OUTPUT_COST_PER_MILLION']]
    except InvalidOperation as exc:
        raise ValueError('approved planner cost rates required') from exc
    if any(not rate.is_finite() or rate < 0 for rate in rates):
        raise ValueError('invalid planner cost rates')


def observed_text_parameters(trace, *, after_evidence=False):
    """Only explicit decision receipts and executed retrieval parents establish order."""
    requests = trace.get('retrievals', [])
    if after_evidence:
        decisions = {row.get('decision_hash'): row for row in trace.get('decisions', []) if row.get('decision_hash') and row.get('next_tool') == 'text_evidence'}
        initial_ids = {row.get('retrieval_id') for row in requests if row.get('phase') == 'initial' and row.get('retrieval_id')}
        executed = []
        for row in requests:
            decision = decisions.get(row.get('origin_decision_hash'))
            if not decision:
                continue
            if row.get('phase') == 'initial' and decision.get('phase') in {'after_metric', 'after_reviews'}:
                executed.append(row)
            elif row.get('phase') == 'model_follow_up' and decision.get('phase') == 'after_text' and row.get('derived_from_retrieval_ids'):
                prior_ids = {parent.get('retrieval_id') for parent in requests if parent.get('phase') == 'initial'}
                if set(row['derived_from_retrieval_ids']) <= prior_ids:
                    executed.append(row)
            elif row.get('phase') == 'counter_evidence' and row.get('derived_from_retrieval_ids') and set(row['derived_from_retrieval_ids']) <= initial_ids:
                # Counter-search must inherit the same decision as its executed parents.
                if all(any(parent.get('retrieval_id') == key and parent.get('origin_decision_hash') == row.get('origin_decision_hash') for parent in requests) for key in row['derived_from_retrieval_ids']):
                    executed.append(row)
        requests = executed
    return [(row.get('phase'), row.get('query'), row.get('evidence_context', 'any')) for row in requests]
