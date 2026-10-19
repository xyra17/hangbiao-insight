"""Approved finite request and evaluation cost bounds; never initialize a provider."""
import json
import os
from decimal import Decimal, InvalidOperation


def amount(name):
    try:
        value = Decimal(os.getenv(name, ''))
    except InvalidOperation as exc:
        raise ValueError('missing approved amount: ' + name) from exc
    if not value.is_finite() or value <= 0:
        raise ValueError('invalid approved amount: ' + name)
    return value


def planner_request_limits(system, user, schema):
    from .execution_config import validate_external_planner_selection
    validate_external_planner_selection()
    if not os.getenv('INVESTIGATION_COST_CURRENCY', '').strip():
        raise ValueError('approved cost currency required')
    max_bytes = int(os.getenv('INVESTIGATION_MAX_REQUEST_BYTES', '12000'))
    max_output = int(os.getenv('INVESTIGATION_MAX_OUTPUT_TOKENS', '3000'))
    if not 1 <= max_bytes <= 65536 or not 1 <= max_output <= 6000:
        raise ValueError('invalid planner request bounds')
    from app.semantic.structured_response import structured_request
    wire = structured_request(model=os.getenv('INVESTIGATION_MODEL', 'deepseek-flash'),
        effort=os.getenv('INVESTIGATION_REASONING_EFFORT', 'none'), name='agent_planner',
        system=system, user=user, schema=schema, max_output_tokens=max_output)
    request_bytes = len(json.dumps(wire, ensure_ascii=False).encode()) + 1024
    if request_bytes > max_bytes:
        raise ValueError('planner request exceeds approved byte bound')
    input_rate = Decimal(os.environ['INVESTIGATION_INPUT_COST_PER_MILLION'])
    output_rate = Decimal(os.environ['INVESTIGATION_OUTPUT_COST_PER_MILLION'])
    upper_cost = (max_bytes * input_rate + max_output * output_rate) / 1_000_000
    if upper_cost > amount('INVESTIGATION_MAX_CALL_COST'):
        raise ValueError('planner request exceeds approved call cost')
    return {'max_output_tokens': max_output, 'max_request_bytes': max_bytes, 'reserved_upper_cost': str(upper_cost)}


def validate_cost_envelope(*, planner_active, relevance_active, max_steps, investigations=None, retrieval_queries=0):
    planner_cap = amount('INVESTIGATION_MAX_CALL_COST') if planner_active else Decimal('0')
    relevance_cap = amount('EVIDENCE_RELEVANCE_MAX_COST') if relevance_active else Decimal('0')
    if not planner_active and not relevance_active:
        return {'external_calls': False, 'upper_cost': '0'}
    if not os.getenv('INVESTIGATION_COST_CURRENCY', '').strip():
        raise ValueError('approved shared cost currency required')
    # Every planner/relevance call within a run consumes the same durable unit ledger.
    run_bound = Decimal(max_steps) * max(planner_cap, relevance_cap)
    if run_bound > amount('INVESTIGATION_MAX_RUN_COST'):
        raise ValueError('durable call allowance exceeds approved run cost')
    evaluation_bound = run_bound * Decimal(investigations or 1) + Decimal(retrieval_queries) * relevance_cap
    if investigations is not None and evaluation_bound > amount('INVESTIGATION_MAX_EVAL_COST'):
        raise ValueError('evaluation exceeds approved total cost')
    return {'external_calls': True, 'run_upper_cost': str(run_bound), 'upper_cost': str(evaluation_bound),
            'currency': os.environ['INVESTIGATION_COST_CURRENCY'], 'limits_are_upper_bounds_not_spend': True}
