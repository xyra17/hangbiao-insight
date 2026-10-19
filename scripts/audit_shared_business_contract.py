"""No-network structural parity and original-request size audit for native-v2."""
import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from quality_round_guard import file_sha, save  # noqa: E402

from app.agent.output_contract import BUSINESS_JSON_SCHEMA  # noqa: E402
from app.agent.planner import LLMPlanner  # noqa: E402
from app.agent.state import InvestigationState  # noqa: E402
from app.semantic.structured_response import structured_request, validate_wire_value  # noqa: E402
from insight.investigation.business_contract_registry import (  # noqa: E402
    FIELDS,
    VERSION,
    fingerprint,
    manifest,
)
from insight.investigation.business_schema import validate_business_value  # noqa: E402
from insight.investigation.model_limits import planner_request_limits  # noqa: E402


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--template-plan', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--protocol', choices=['native-v2', 'native-v3'], default='native-v2')
    args = parser.parse_args()
    plan = json.loads(args.template_plan.read_text())
    os.environ.update(plan['environment'])
    os.environ.update(INVESTIGATION_PLANNER_PROTOCOL=args.protocol, INVESTIGATION_REGION='local-no-network',
                      INVESTIGATION_APPROVAL_REFERENCE='local-wire-audit-only')
    checked = 0
    for field, (_, values) in FIELDS.items():
        candidates = ['unknown', *values] if field != 'expected_tool_names' else ['unknown', [], *[[v] for v in values]]
        for value in candidates:
            validate_wire_value(value, BUSINESS_JSON_SCHEMA['properties'][field])
            assert validate_business_value(field, value) == value
            checked += 1
    counterexamples = []
    for field, value in [('expected_tool_names', 'not_json'), ('expected_route', 'invented_but_valid_label')]:
        outcomes = {}
        for name, validate in [('wire', lambda: validate_wire_value(value, BUSINESS_JSON_SCHEMA['properties'][field])),
                               ('parser', lambda: validate_business_value(field, value))]:
            try:
                validate()
                outcomes[name] = 'accepted'
            except ValueError as exc:
                outcomes[name] = str(exc)
        assert all(v != 'accepted' for v in outcomes.values())
        counterexamples.append({'field': field, 'value': value, 'outcomes': outcomes})
    requests = []
    for round_name, numbers in [('internal-demo-real-quality-20261013-segments-v4', [31, 32]),
                                ('internal-demo-real-quality-20261013-staged-v7', [1, 3, 5]),
                                ('internal-demo-real-quality-20261013-shared-contract-v9', [1, 5, 10]),
                                ('internal-demo-real-quality-20261014-addressed-final-v11', [1, 3]),
                                ('internal-demo-real-quality-20261014-request64k-v13-final', [1, 2])]:
        directory = ROOT / '.local/acceptance-artifacts' / round_name / 'paid'
        seal = json.loads((directory / 'seal.json').read_text())
        for index in numbers:
            name = f'request-{index:04}.json'
            assert file_sha(directory / name) == seal[name]
            original = json.loads((directory / name).read_text())
            user = json.loads(original['input'][1]['content'])
            class Gateway:
                execution_origin = 'controlled_fixture'
                def plan(self, **kwargs):
                    bounds = planner_request_limits(kwargs['system'], kwargs['user'], kwargs['schema'])
                    wire = structured_request(model=plan['model'], effort='none', name='agent_planner',
                        system=kwargs['system'], user=kwargs['user'], schema=kwargs['schema'], max_output_tokens=bounds['max_output_tokens'])
                    size = len(json.dumps(wire, ensure_ascii=False).encode()) + 1024
                    if size > plan['budget']['calls']['planner']['request_bytes']:
                        raise ValueError(f'wire_byte_cap_exceeded:{round_name}:{index}:{size}')
                    requests.append({'round': round_name, 'original_request': name, 'original_sha256': seal[name],
                        'native_request_bytes_plus_margin': size, 'under_unchanged_byte_cap': True})
                    return {'decision': 'clarify', 'tool_name': None, 'arguments': {'query': None, 'evidence_context': None},
                        'decision_summary': 'local no-network format audit', 'target_hypothesis_id': None, 'stop_reason': None,
                        'final_answer': None, 'confidence': None, 'missing_fields': ['business_goal'], 'counter_evidence_query': ''}
            state = InvestigationState(request_id='original-wire-audit', user_query=user['query'], product_id=user['product_id'],
                period=user['period'], frozen_scope=user['frozen_scope'], observations=user['observations'],
                hypotheses=user['hypotheses'], step_count=user['step_count'])
            LLMPlanner(Gateway()).decide(state, tuple(user['available_tools']))
    save(args.output, {'status': 'local_contract_audit_passed', 'version': VERSION, 'registry_sha256': fingerprint(),
        'external_calls': 0, 'registered_value_checks': checked, 'counterexamples': counterexamples,
        'original_request_size_checks': requests, 'quality_accepted': False,
        'vocabulary': manifest(), 'supplier_native_protocol_validation': 'not_run'})
    print(json.dumps({'checked_values': checked, 'original_requests': len(requests), 'external_calls': 0}))


if __name__ == '__main__':
    main()
