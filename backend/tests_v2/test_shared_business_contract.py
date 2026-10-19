"""Finite product vocabulary and structural parity; no eval gold is read."""
import copy
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from app.agent.output_contract import (  # noqa: E402
    BUSINESS_JSON_SCHEMA,
    CURRENT_FINAL_SCHEMA_VERSION,
    validate_final_answer,
)
from app.agent.planner import LLMPlanner, planner_schema  # noqa: E402
from app.agent.state import InvestigationState  # noqa: E402
from app.semantic.structured_response import validate_wire_value  # noqa: E402
from insight.investigation.business_contract_registry import (  # noqa: E402
    FIELDS,
    VERSION,
    fingerprint,
    manifest,
    review_text,
)
from insight.investigation.business_schema import parse_review_value, validate_business_value  # noqa: E402
from insight.investigation.evaluation import adapt_prediction, write_template  # noqa: E402
from insight.investigation.output import compose_output  # noqa: E402


def contract():
    return {'schema_version': VERSION, 'expected_route': 'feedback_review',
        'expected_behavior': 'separate_occurrence_and_cause', 'expected_next_step': 'verify_product_and_usage',
        'allowed_conclusion': 'case_level_feedback', 'forbidden_conclusion': 'unproven_causal_claim',
        'safety_escalation': 'no_signal_in_examined_evidence', 'expected_stop_condition': 'evidence_scope_complete',
        'expected_tool_names': ['search_feedback_evidence']}


def terminal():
    return {'decision': 'stop', 'tool_name': None, 'arguments': {'query': None, 'evidence_context': None},
        'decision_summary': 'Controlled protocol validation', 'target_hypothesis_id': None, 'stop_reason': 'evidence_scope_complete',
        'confidence': None, 'missing_fields': [], 'counter_evidence_query': '', 'final_answer': {
            'schema_version': CURRENT_FINAL_SCHEMA_VERSION, 'statement': 'One original reports damage; cause unconfirmed.',
            'citations': [{'fact_id': 'f', 'quote': 'damaged', 'source_field': 'text'}], 'business_contract': contract()}}


@pytest.mark.parametrize('field', list(FIELDS))
def test_every_registered_value_has_same_wire_and_parser_type(field):
    values = ['unknown', *FIELDS[field][1]] if field != 'expected_tool_names' else ['unknown', [], *[[v] for v in FIELDS[field][1]], list(FIELDS[field][1])]
    for value in values:
        validate_wire_value(value, BUSINESS_JSON_SCHEMA['properties'][field])
        assert validate_business_value(field, value) == value
        encoded = json.dumps(value) if isinstance(value, list) else value
        assert parse_review_value(field, encoded, version=VERSION) == value


@pytest.mark.parametrize('field,value', [('expected_tool_names', 'not_json'), ('expected_tool_names', '["search_feedback_evidence"]'),
    ('expected_tool_names', ['external_price_change']), ('expected_route', 'invented_but_valid_label'),
    ('expected_route', '调查评论'), ('expected_route', None)])
def test_counterexamples_rejected_by_both_structural_layers(field, value):
    with pytest.raises(ValueError):
        validate_wire_value(value, BUSINESS_JSON_SCHEMA['properties'][field])
    with pytest.raises(ValueError):
        validate_business_value(field, value)


def test_current_wire_is_native_and_has_no_pattern_dependency():
    schema = planner_schema('native-v2')
    assert schema['properties']['arguments']['type'] == 'object'
    assert schema['properties']['missing_fields']['type'] == 'array'
    assert 'pattern' not in json.dumps(schema)
    assert 'invented_but_valid_label' not in review_text()
    assert manifest()['version'] == VERSION and len(fingerprint()) == 64


def test_native_terminal_through_model_adapter_result_review_and_score(monkeypatch, tmp_path):
    import csv

    from score_real_review_quality import investigation_score

    from insight.investigation.evaluation import trace_hash
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v2')
    evidence = [{'type': 'text_retrieval', 'items': [{'fact_id': 'f', 'text': 'Box damaged.', 'match_kind': 'relevant_hit'}]}]
    class Gateway:
        execution_origin = 'controlled_fixture'
        def plan(self, **request):
            assert fingerprint() in request['system']
            return terminal()
    decision = LLMPlanner(Gateway()).decide(InvestigationState(request_id='local', user_query='Check',
        observations=[{'data': evidence[0]}]), ())
    parsed = json.loads(decision.final_answer)
    result = compose_output(evidence, [{'final_answer': parsed['statement'], 'conclusion': parsed,
                                      'business_contract': parsed['business_contract']}])
    assert result['business_schema_version'] == VERSION
    traces = [{'scenario_id': f'local-{i}', 'status': 'completed', 'tools': [], 'result': copy.deepcopy(result)} for i in range(48)]
    prediction, metadata = adapt_prediction(traces[0])
    assert prediction['expected_tool_names'] == ['search_feedback_evidence']
    assert metadata['field_statuses']['expected_tool_names'] == 'model_proposal'
    write_template(tmp_path / 'investigation.csv', traces)
    rows = list(csv.DictReader((tmp_path / 'investigation.csv').open()))
    assert all(row['expected_route'] == 'unknown' for row in rows)  # no model answers in form
    # Controlled scoring self-test explicitly separate from independent quality.
    for row, trace in zip(rows, traces, strict=True):
        for field in FIELDS:
            value = contract()[field]
            row[field] = json.dumps(value) if isinstance(value, list) else value
        row.update(reviewer='controlled', reviewed_at='2026-10-13T12:00:00+08:00', adjudication_kind='independent_human')
        row['trace_sha256'] = trace_hash(trace)
    with (tmp_path / 'investigation.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    runtime = {'investigation': {'trajectories': traces}}
    from score_real_review_quality import canonical_hash
    (tmp_path / 'manifest.json').write_text(json.dumps({'runtime_sha256': canonical_hash(runtime)}))
    assert investigation_score(runtime, tmp_path)['matched_fields'] == 384
    assert investigation_score(runtime, tmp_path)['status'] == 'pending_independent_statement_review'
    for row in rows:
        row['statement_correct'] = 'true'
    rows[0]['expected_route'] = 'metric_review'
    with (tmp_path / 'investigation.csv').open('w', newline='') as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    assert investigation_score(runtime, tmp_path)['matched_fields'] == 383


@pytest.mark.parametrize('label', ['case_level_feedback', 'conflicting_feedback', 'cited_metric_fact'])
def test_evidence_semantics_are_separate_from_structural_validation(label):
    answer = terminal()['final_answer']
    answer['citations'] = []
    answer['business_contract']['allowed_conclusion'] = label
    validate_wire_value(answer, planner_schema('native-v2')['properties']['final_answer'])
    with pytest.raises(ValueError):
        validate_final_answer(answer, [], protocol='native-v2')


@pytest.mark.parametrize('name', ['expected_route', 'allowed_conclusion', 'expected_tool_names'])
def test_blank_or_null_does_not_become_unknown(name):
    for value in ['', None, {}]:
        with pytest.raises(ValueError):
            validate_wire_value(value, BUSINESS_JSON_SCHEMA['properties'][name])
        with pytest.raises(ValueError):
            validate_business_value(name, value)


def test_old_contract_is_not_semantically_translated():
    from insight.investigation.business_schema import BUSINESS_SCHEMA_VERSION
    assert validate_business_value('expected_route', 'invented_but_valid_label', version=BUSINESS_SCHEMA_VERSION) == 'invented_but_valid_label'
    assert validate_business_value('expected_tool_names', '["legacy_capability"]', version=BUSINESS_SCHEMA_VERSION) == '["legacy_capability"]'
    with pytest.raises(ValueError):
        validate_business_value('expected_route', 'invented_but_valid_label', version=VERSION)


def test_v7_original_terminal_is_read_only_and_not_auto_translated():
    from quality_round_guard import file_sha
    root = Path(__file__).resolve().parents[2] / '.local/acceptance-artifacts/internal-demo-real-quality-20261013-staged-v7/paid'
    if not root.exists():
        pytest.skip('local original unavailable')
    seal = json.loads((root / 'seal.json').read_text())
    for name in ['response-0005.json', 'trajectory-01.json']:
        assert file_sha(root / name) == seal[name]
    raw = json.loads((root / 'response-0005.json').read_text())
    payload = json.loads(next(c['text'] for o in raw['output'] for c in o.get('content', []) if c.get('text')))
    with pytest.raises(ValueError):
        validate_wire_value(payload, planner_schema('native-v2'))
    # The old model labels remain Chinese/free labels in their original receipt.
    assert payload['final_answer']['business_contract']['schema_version'] != VERSION
    assert payload['final_answer']['business_contract']['expected_behavior'].startswith('基于')


@pytest.mark.parametrize('mode', ['clarify', 'stop', 'call_tool'])
def test_native_decision_types_pass_same_wire_and_pydantic_boundary(monkeypatch, mode):
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v2')
    value = terminal()
    if mode == 'clarify':
        value.update(decision='clarify', final_answer=None, missing_fields=['business_goal'])
    elif mode == 'call_tool':
        value.update(decision='call_tool', tool_name='text_evidence', final_answer=None,
                     arguments={'query': 'charging feedback', 'evidence_context': 'any'}, stop_reason=None)
    else:
        value['final_answer']['citations'] = []
        value['final_answer']['business_contract'] = {'schema_version': VERSION, **{k: 'unknown' for k in FIELDS}}
    validate_wire_value(value, planner_schema('native-v2'))
    class Gateway:
        execution_origin = 'controlled_fixture'
        def plan(self, **kw):
            return value
    result = LLMPlanner(Gateway()).decide(InvestigationState(request_id='types', user_query='Inspect'), ('text_evidence',))
    assert result.decision == mode


def test_tool_scope_and_permission_rejections_are_explicit(monkeypatch):
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v2')
    value = terminal()
    value.update(decision='call_tool', tool_name='external_price_change', final_answer=None, stop_reason=None)
    class Gateway:
        def plan(self, **kw):
            return value
    validate_wire_value(value, planner_schema('native-v2'))  # permission is separate from structure
    with pytest.raises(ValueError, match='unauthorized tool'):
        LLMPlanner(Gateway()).decide(InvestigationState(request_id='denied', user_query='Inspect'), ('text_evidence',))
    value.update(tool_name='metric_snapshot', arguments={'query': 'forged_scope', 'evidence_context': 'any'})
    with pytest.raises(ValueError, match='outside_declared_capability'):
        LLMPlanner(Gateway()).decide(InvestigationState(request_id='scope', user_query='Inspect'), ('metric_snapshot',))


def test_native_confidence_range_matches_pydantic_structure():
    from app.agent.investigation import AgentDecision
    schema = planner_schema('native-v2')['properties']['confidence']
    for confidence in [None, 0, .55, 1]:
        validate_wire_value(confidence, schema)
        AgentDecision(decision='stop', stop_reason='scope', confidence=confidence)
    for confidence in [-.1, 1.1]:
        with pytest.raises(ValueError):
            validate_wire_value(confidence, schema)
        with pytest.raises(ValueError):
            AgentDecision(decision='stop', stop_reason='scope', confidence=confidence)
