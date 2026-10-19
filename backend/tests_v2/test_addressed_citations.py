import copy
import json

import pytest

from app.agent.planner import LLMPlanner
from app.agent.state import InvestigationState
from insight.investigation.addressed_citations import address_observations, bind_answer, citation_schema
from insight.investigation.citations import metric_catalog, validate_citations
from tests_v2.test_shared_business_contract import terminal


def observations():
    return [{'data': {'type': 'text_retrieval', 'items': [
        {'fact_id': 'f', 'title': 'works flawlessly', 'text': 'OrigINal "typoo".', 'match_kind': 'relevant_hit'},
        {'fact_id': 'context', 'text': 'not evidence', 'match_kind': 'scope_context'}]}}]


def test_original_field_binding():
    original = observations()
    before = copy.deepcopy(original)
    view, bindings = address_observations(original)
    ref = view[0]['data']['items'][0]['citation_refs']['title']
    answer, receipts = bind_answer({'citations': [{'citation_ref': ref}]}, bindings)
    assert answer['citations'] == [{'fact_id': 'f', 'quote': 'works flawlessly', 'source_field': 'title'}]
    assert len(receipts[0]['original_sha256']) == 64
    assert original == before and view[0]['data']['items'][1]['citation_refs'] == {}
    assert bindings[view[0]['data']['items'][0]['citation_refs']['text']]['quote'] == 'OrigINal "typoo".'
    validate_citations(answer['citations'], [original[0]['data']])
    with pytest.raises(ValueError):
        validate_citations([{'fact_id': 'f', 'quote': 'works flawlessly', 'source_field': 'text'}], [original[0]['data']])


@pytest.mark.parametrize('item', [{'citation_ref': 'forged'}, {'citation_ref': 'no_available_reference'},
    {'citation_ref': 'ref_1', 'source_field': 'text'}, {'fact_id': 'f', 'quote': 'works flawlessly', 'source_field': 'title'}])
def test_rejects_invalid_reference(item):
    _, bindings = address_observations(observations())
    with pytest.raises(ValueError):
        bind_answer({'citations': [item]}, bindings)
    assert citation_schema({})['maxItems'] == 0


def test_metric_exact_atom():
    catalog = metric_catalog(snapshot_id='s', snapshot_fingerprint='fp', source_manifest={}, shop_id='shop',
        child_asin='asin', product_id=None, metrics={'current': {'start': '2026-01-01', 'end': '2026-01-02', 'rate': .1, 'denominator': 10}})
    original = [{'data': {'type': 'metric_snapshot', 'metric_evidence': catalog}}]
    view, bindings = address_observations(original)
    ref = view[0]['data']['metric_evidence']['atoms'][2]['citation_ref']
    answer, _ = bind_answer({'citations': [{'citation_ref': ref}]}, bindings)
    assert answer['citations'][0] == catalog['atoms'][2]
    validate_citations(answer['citations'], [original[0]['data']])


def test_planner_v3_receipt(monkeypatch):
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v3')
    class Gateway:
        execution_origin = 'controlled_fixture'
        last_model_receipt = {'raw_response_sha256': 'unchanged'}
        def plan(self, **request):
            assert 'agent-final-output-v5' not in request['system']
            view = json.loads(request['user'])['observations']
            value = terminal()
            value['final_answer']['schema_version'] = 'agent-final-output-v6'
            value['final_answer']['citations'] = [{'citation_ref': view[0]['data']['items'][0]['citation_refs']['title']}]
            return value
    gateway = Gateway()
    decision = LLMPlanner(gateway).decide(InvestigationState(request_id='local', user_query='Check', observations=observations()), ())
    assert json.loads(decision.final_answer)['citations'][0]['source_field'] == 'title'
    assert gateway.last_model_receipt['raw_response_sha256'] == 'unchanged'
    assert gateway.last_model_receipt['citation_bindings']


def test_resolved_reference_still_checks_date_and_metric_identity():
    original = observations()
    original[0]['data']['request_scope'] = {'status': 'explicit', 'start': '2026-01-01', 'end': '2026-01-02'}
    original[0]['data']['items'][0]['business_date'] = '2025-01-01'
    view, bindings = address_observations(original)
    answer, _ = bind_answer({'citations': [{'citation_ref': view[0]['data']['items'][0]['citation_refs']['title']}]}, bindings)
    with pytest.raises(ValueError, match='outside requested date scope'):
        validate_citations(answer['citations'], [original[0]['data']])
    catalog = metric_catalog(snapshot_id='s', snapshot_fingerprint='fp', source_manifest={}, shop_id='shop',
        child_asin='asin', product_id=None, metrics={'units': 10})
    altered = copy.deepcopy(catalog['atoms'][0])
    altered['snapshot_fingerprint'] = 'forged'
    with pytest.raises(ValueError, match='differs from frozen'):
        validate_citations([altered], [{'type': 'metric_snapshot', 'metric_evidence': catalog}])


def test_native_v3_schema_removes_only_duplicate_prose():
    from app.agent.output_contract import final_schema
    from app.agent.planner import planner_schema
    wire = planner_schema('native-v3', {'ref_1': {}})
    original = final_schema('native-v3', {'ref_1': {}})
    def structural(node):
        if isinstance(node, dict):
            return {k: structural(v) for k, v in node.items() if k != 'description'}
        if isinstance(node, list):
            return [structural(v) for v in node]
        return node
    assert wire['properties']['final_answer'] == structural(original)


def test_planner_limit_counts_full_wire_not_only_content(monkeypatch):
    from insight.investigation.model_limits import planner_request_limits
    monkeypatch.setenv('INSIGHT_ENABLED_MODEL_PROVIDERS', 'deepseek')
    monkeypatch.setenv('INVESTIGATION_APPROVAL_REFERENCE', 'controlled-no-network')
    monkeypatch.setenv('INVESTIGATION_REGION', 'local')
    monkeypatch.setenv('INVESTIGATION_COST_CURRENCY', 'USD')
    monkeypatch.setenv('INVESTIGATION_MAX_REQUEST_BYTES', '24000')
    monkeypatch.setenv('INVESTIGATION_MAX_OUTPUT_TOKENS', '3000')
    monkeypatch.setenv('INVESTIGATION_INPUT_COST_PER_MILLION', '.3')
    monkeypatch.setenv('INVESTIGATION_OUTPUT_COST_PER_MILLION', '1.2')
    monkeypatch.setenv('INVESTIGATION_MAX_CALL_COST', '.0108')
    monkeypatch.setenv('INVESTIGATION_MODEL', 'deepseek-flash')
    monkeypatch.setenv('INVESTIGATION_REASONING_EFFORT', 'none')
    # Old content-only calculation fits, but wrapper and JSON escaping do not.
    schema = {'type': 'string'}
    user = '"' * 12000
    assert len((user + json.dumps(schema)).encode()) + 1024 < 24000
    with pytest.raises(ValueError, match='approved byte bound'):
        planner_request_limits('', user, schema)


def test_failed_representative_does_not_overwrite_original_fault(monkeypatch, tmp_path):
    import real_review_stage_gate
    from quality_round_guard import RoundGuard
    monkeypatch.setattr(real_review_stage_gate, 'representative_gate', lambda *a, **kw: {'execution_gate_passed': False})
    guard = RoundGuard.__new__(RoundGuard)
    guard.plan = {'execution_stages': {'representative_id': 'test'}, 'date_scope': {}}
    guard.paid = True
    guard.directory = tmp_path
    guard.stopped = 'request_preflight_failed'
    with pytest.raises(RuntimeError, match='request_preflight_failed'):
        guard.unlock_remaining({'scenario_id': 'test'})
    assert guard.stopped == 'request_preflight_failed'


def test_empty_evidence_schema_accepts_only_empty_citations():
    from jsonschema import ValidationError, validate
    schema = citation_schema({})
    assert schema['items']['properties']
    assert 'no_available_reference' not in json.dumps(schema)
    validate([], schema)
    for invalid in [[{}], [{'citation_ref': 'no_available_reference'}], [{'citation_ref': 'ref_1'}]]:
        with pytest.raises(ValidationError):
            validate(invalid, schema)
    assert bind_answer({'citations': []}, {}) == ({'citations': []}, [])


def test_counter_evidence_absence_keeps_string_contract():
    from jsonschema import ValidationError, validate

    from app.agent.planner import planner_schema
    schema = planner_schema('native-v3', {})['properties']['counter_evidence_query']
    validate('', schema)
    with pytest.raises(ValidationError):
        validate(None, schema)


def test_production_wire_validator_enforces_empty_citation_array():
    from app.semantic.structured_response import validate_wire_value
    schema = citation_schema({})
    validate_wire_value([], schema)
    with pytest.raises(ValueError, match='wire_array_length_mismatch'):
        validate_wire_value([{'citation_ref': 'forged'}], schema)


def test_no_available_tools_schema_cannot_request_tool(monkeypatch):
    monkeypatch.setenv('INVESTIGATION_PLANNER_PROTOCOL', 'native-v3')
    class Gateway:
        execution_origin = 'controlled_fixture'
        last_model_receipt = {}
        def plan(self, **request):
            schema = request['schema']
            assert schema['properties']['tool_name']['enum'] == [None]
            assert schema['properties']['decision']['enum'] == ['stop', 'clarify']
            value = terminal()
            value['final_answer']['schema_version'] = 'agent-final-output-v6'
            value['final_answer']['citations'] = []
            value['final_answer']['business_contract']['allowed_conclusion'] = 'incomplete_evidence'
            return value
    assert LLMPlanner(Gateway()).decide(InvestigationState(request_id='none', user_query='Inspect'), ()).decision == 'stop'


def test_guard_preserves_malformed_completed_response(tmp_path):
    from decimal import Decimal
    from types import SimpleNamespace

    from quality_round_guard import RoundGuard
    guard = RoundGuard.__new__(RoundGuard)
    guard.plan = {'model': 'deepseek-flash', 'budget': {'calls': {'planner': {'request_bytes': 24000, 'output_tokens': 3000, 'upper_usd': '.01'}}, 'investigation_call_units': 12, 'max_calls': 906, 'total_upper_usd': '15'}}
    guard.paid = True
    guard.directory = tmp_path
    guard.group = 'investigation:test'
    guard.stage_unlocked = True
    guard.stopped = None
    guard.receipts, guard.counts, guard.reserved = [], {}, Decimal(0)
    raw = {'status': 'completed', 'id': 'broken', 'model': 'deepseek-flash', 'usage': {'input_tokens': 20, 'output_tokens': 20}, 'output_text': '{"decision":"stop"'}
    response = SimpleNamespace(status='completed', output_text=raw['output_text'], model_dump=lambda **kw: raw)
    guard.constructor = lambda **kw: SimpleNamespace(responses=SimpleNamespace(create=lambda **kw: response))
    client = guard.client(base_url='https://api.deepseek.com', max_retries=0)
    with pytest.raises(RuntimeError, match='provider_response_json_invalid'):
        client.responses.create(model='deepseek-flash', reasoning={'effort': 'none'}, max_output_tokens=3000, text={'format': {'name': 'agent_planner', 'strict': True, 'schema': {'type': 'string'}}})
    assert guard.stopped == 'provider_response_json_invalid'
    assert json.loads((tmp_path / 'response-0001.json').read_text()) == raw
    assert guard.receipts[0]['response_received'] is True


def test_explicit_sampling_temperature_is_in_wire(monkeypatch):
    from app.semantic.structured_response import structured_request
    kwargs = dict(model='deepseek-flash', effort='none', name='test', system='rules', user='data', schema={'type': 'string'}, max_output_tokens=3000)
    monkeypatch.delenv('STRUCTURED_RESPONSE_TEMPERATURE', raising=False)
    assert 'temperature' not in structured_request(**kwargs)
    monkeypatch.setenv('STRUCTURED_RESPONSE_TEMPERATURE', '0')
    assert structured_request(**kwargs)['temperature'] == 0
    for invalid in ['-1', '3', 'nan']:
        monkeypatch.setenv('STRUCTURED_RESPONSE_TEMPERATURE', invalid)
        with pytest.raises(ValueError):
            structured_request(**kwargs)


def test_classification_schema_enforces_parent_child_pair():
    from jsonschema import ValidationError, validate

    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import Taxonomy
    taxonomy = Taxonomy(version='taxonomy-v1.1', status='frozen', topic_structure={'usability_installation': ('ease_of_use',), 'convenience_value': ('price_value',)})
    package = PromptBuilder(taxonomy, prompt_version='semantic-prompt-v1.3').build(ReviewInput(review_id='local', product_id='local', text='Just lay the phone on these chargers.'))
    claim = {'primary_topic': 'usability_installation', 'secondary_topic': 'ease_of_use', 'polarity': 'positive', 'evidence_span': 'Just lay the phone'}
    validate({'claims': [claim]}, package.response_schema)
    claim['primary_topic'] = 'convenience_value'
    with pytest.raises(ValidationError):
        validate({'claims': [claim]}, package.response_schema)


def test_classification_original_sentence_cannot_delete_internal_words():
    from jsonschema import ValidationError, validate

    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import Taxonomy
    taxonomy = Taxonomy(version='taxonomy-v1.1', status='frozen', topic_structure={'reliability_durability': ('general_durability',)})
    original = 'This has been so easy and reliable so far -- it charges in the house! Love.'
    package = PromptBuilder(taxonomy, prompt_version='semantic-prompt-v1.4').build(ReviewInput(review_id='local', product_id='local', text=original))
    choices = package.response_schema['$defs']['OriginalEvidenceSpan']['enum']
    assert choices and all(c in original for c in choices)
    claim = {'primary_topic': 'reliability_durability', 'secondary_topic': 'general_durability', 'polarity': 'positive', 'evidence_span': choices[0]}
    validate({'claims': [claim]}, package.response_schema)
    claim['evidence_span'] = 'so reliable so far'
    with pytest.raises(ValidationError):
        validate({'claims': [claim]}, package.response_schema)


def test_explicit_classification_pair_binding_preserves_original_response():
    from app.semantic.model_gateway import bind_extraction_payload
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import Taxonomy
    taxonomy = Taxonomy(version='taxonomy-v1.1', status='frozen', topic_structure={'reliability_durability': ('general_durability',)})
    source = 'This has been so easy and reliable so far.'
    prompt = PromptBuilder(taxonomy, prompt_version='semantic-prompt-v1.5').build(ReviewInput(review_id='local', product_id='local', text=source))
    value = {'claims': [{'topic_pair': 'reliability_durability/general_durability', 'polarity': 'positive', 'evidence_span': source}]}
    raw = json.dumps(value)
    expected = {'claims': [{'primary_topic': 'reliability_durability', 'secondary_topic': 'general_durability', 'polarity': 'positive', 'evidence_span': source}]}
    assert json.loads(bind_extraction_payload(raw, prompt)) == expected
    assert json.loads(raw) == value
    for changes in [{'topic_pair': 'invented/pair'}, {'evidence_span': 'so reliable so far'}, {'polarity': 'invented'}, {'extra': 'forged'}]:
        invalid = copy.deepcopy(value)
        invalid['claims'][0].update(changes)
        with pytest.raises(ValueError):
            bind_extraction_payload(json.dumps(invalid), prompt)


def test_classification_reference_binds_exact_original_without_model_rewriting():
    from app.semantic.model_gateway import bind_extraction_payload
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import Taxonomy
    taxonomy = Taxonomy(version='taxonomy-v1.1', status='frozen', topic_structure={'reliability_durability': ('short_lifespan',)})
    source = 'Updated. Worth a lot<br /><br />I have had this less than 6 months.'
    prompt = PromptBuilder(taxonomy, prompt_version='semantic-prompt-v1.6').build(ReviewInput(review_id='local', product_id='local', text=source))
    ref = list(prompt.evidence_bindings)[-1]
    raw = json.dumps({'claims': [{'topic_pair': 'reliability_durability/short_lifespan', 'polarity': 'negative', 'evidence_ref': ref}]})
    result = json.loads(bind_extraction_payload(raw, prompt))
    assert result['claims'][0]['evidence_span'] == prompt.evidence_bindings[ref]
    assert result['claims'][0]['evidence_span'] in source
    invalid = json.loads(raw)
    invalid['claims'][0]['evidence_ref'] = 'invented'
    with pytest.raises(ValueError):
        bind_extraction_payload(json.dumps(invalid), prompt)


def test_classification_topic_and_evidence_ids_bind_only_frozen_choices():
    from app.semantic.model_gateway import bind_extraction_payload
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import Taxonomy
    taxonomy = Taxonomy(version='taxonomy-v1.1', status='frozen', topic_structure={'compatibility_fit': ('case_compatibility',), 'usability_installation': ('positioning_alignment',)})
    source = 'Works without the case. Needs a sweet spot with the case.'
    prompt = PromptBuilder(taxonomy, prompt_version='semantic-prompt-v1.7').build(ReviewInput(review_id='local', product_id='local', text=source))
    value = {'claims': [{'topic_ref': 'topic_2', 'polarity': 'negative', 'evidence_ref': 'original_2'}]}
    raw = json.dumps(value)
    assert json.loads(bind_extraction_payload(raw, prompt)) == {'claims': [{'primary_topic': 'usability_installation', 'secondary_topic': 'positioning_alignment', 'polarity': 'negative', 'evidence_span': 'Needs a sweet spot with the case.'}]}
    assert json.loads(raw) == value
    assert json.loads(prompt.user)['legal_topic_choices'][1]['secondary_topic'] == 'positioning_alignment'
    for changes in [{'topic_ref': 'topic_999'}, {'evidence_ref': 'original_999'}, {'topic_pair': 'compatibility_fit/positioning_alignment'}]:
        invalid = copy.deepcopy(value)
        invalid['claims'][0].update(changes)
        with pytest.raises(ValueError):
            bind_extraction_payload(json.dumps(invalid), prompt)
