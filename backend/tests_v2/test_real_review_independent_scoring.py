"""Controlled scoring regressions; fixtures are never real-model quality evidence."""

import csv
import importlib.util
import json
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parents[2] / 'scripts'
sys.path.insert(0, str(SCRIPTS))
spec = importlib.util.spec_from_file_location('real_review_score', SCRIPTS / 'score_real_review_quality.py')
scorer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scorer)


def metadata():
    return {'reviewer': 'controlled-test-fixture', 'reviewed_at': '2026-10-13T12:00:00+08:00', 'review_status': 'reviewed', 'adjudication_kind': 'independent_human'}


def write_csv(path, rows):
    with path.open('w', newline='', encoding='utf-8') as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


@pytest.fixture
def classification():
    text = 'It charges slowly.'
    claim = {'primary_topic': 'function_performance', 'secondary_topic': 'charging_speed', 'polarity': 'negative', 'evidence_span': text}
    source = {'classification': [{'review_id': 'actual-outer-id', 'text': text}]}
    labels = {'actual-outer-id': {'text': text, 'claims_json': json.dumps([claim]), **metadata()}}
    runtime = {'classification': {'predictions': [{'review_id': 'actual-outer-id', 'guarded': {
        'rejected_count': 0, 'predictions': [{'review_id': 'runtime-review', 'prediction_status': 'accepted', **claim}]}}]}}
    taxonomy = scorer.TaxonomyLoader.load(Path(__file__).resolve().parents[2] / 'dataset/data/03_黄金评论集/主题分类体系_v1_1.json')
    return source, labels, runtime, taxonomy


def test_classification_maps_constant_model_identity_to_frozen_outer_id(classification):
    report = scorer.classification_score(*classification)
    assert report['status'] == 'passed_for_observed_topics'
    assert report['observed_primary_macro_f1'] == 1
    assert report['unsupported_primary_topics']
    assert report['unsupported_secondary_topics']


@pytest.mark.parametrize('value', ['', 'unknown'])
def test_unreviewed_or_unknown_is_not_empty_gold(classification, value):
    source, labels, runtime, taxonomy = classification
    labels['actual-outer-id']['review_status'] = value
    result = scorer.classification_score(source, labels, runtime, taxonomy)
    assert result['status'] == 'pending_independent_review' and result['metrics'] is None


def test_empty_claims_do_not_pass_vacuously(classification):
    source, labels, runtime, taxonomy = classification
    labels['actual-outer-id']['claims_json'] = '[]'
    runtime['classification']['predictions'][0]['guarded']['predictions'] = []
    assert scorer.classification_score(source, labels, runtime, taxonomy)['status'] == 'failed'


def test_invalid_gold_quote_rejected(classification):
    source, labels, runtime, taxonomy = classification
    claim = json.loads(labels['actual-outer-id']['claims_json'])
    claim[0]['evidence_span'] = 'invented exact quote'
    labels['actual-outer-id']['claims_json'] = json.dumps(claim)
    with pytest.raises(ValueError, match='polarity/evidence'):
        scorer.classification_score(source, labels, runtime, taxonomy)


@pytest.fixture
def retrieval():
    docs = [{'record_id': 'doc', 'child_asin': 'ASIN', 'title': 'Slow', 'text': 'It charges slowly.', 'business_date': '2023-08-01'}]
    queries = [{'query_id': f'q{i}', 'query': f'question {i}', 'child_asin': 'ASIN', 'split': split}
               for i, split in enumerate(['development', 'development', 'validation', 'validation', 'test', 'test'])]
    answers = {q['query_id']: {'answerable': str(i % 2 == 0).lower(), **metadata()} for i, q in enumerate(queries)}
    qrels = {(q['query_id'], 'doc'): {'relevant': str(i % 2 == 0).lower(), 'supports_occurrence': 'true', 'supports_causality': 'false', **metadata()} for i, q in enumerate(queries)}
    item = {**docs[0], 'raw_row_id': 'raw', 'fact_id': 'fact', 'match_kind': 'relevant_hit'}
    outputs = {q['query_id']: {'query': q, 'scope': {'asin': 'ASIN'}, 'items': [dict(item)] if i % 2 == 0 else [],
                'relevance_judgment': {'decisions': [{'candidate_id': 'fact', 'label': 'relevant', 'source_field': 'text',
                    'start': 0, 'end': 18, 'quote': 'It charges slowly.'}]}, 'retrieval_complete': True, 'latency_ms': 100 + i}
               for i, q in enumerate(queries)}
    return {'queries': queries, 'corpus': docs}, answers, qrels, {'retrieval': {'per_query': outputs}}


def test_retrieval_split_recall_noanswer_citation_and_observed_latency(retrieval):
    report = scorer.retrieval_score(*retrieval)
    assert report['status'] == 'passed'
    for row in report['splits'].values():
        assert row['recall_at_10'] == 1 and row['citation_validity'] == 1
        assert row['no_answer_query_count'] == 1 and row['latency_p95_ms'] > 0


def test_unknown_qrel_prevents_full_retrieval_score(retrieval):
    source, answers, qrels, runtime = retrieval
    qrels[('q0', 'doc')]['relevant'] = 'unknown'
    result = scorer.retrieval_score(source, answers, qrels, runtime)
    assert result['status'] == 'pending_independent_review' and result['splits'] == {}


def test_noanswer_false_hit_and_modified_citation_fail(retrieval):
    source, answers, qrels, runtime = retrieval
    runtime['retrieval']['per_query']['q1']['items'] = runtime['retrieval']['per_query']['q0']['items']
    runtime['retrieval']['per_query']['q0']['items'][0]['text'] = 'model corrected text'
    report = scorer.retrieval_score(source, answers, qrels, runtime)
    assert report['status'] == 'failed_or_unsupported'
    assert report['splits']['development']['no_answer_false_retrievals'] == 1
    assert report['splits']['development']['citation_validity'] == 0


def test_answerability_cannot_contradict_full_corpus_qrels(retrieval):
    source, answers, qrels, runtime = retrieval
    answers['q0']['answerable'] = 'false'
    with pytest.raises(ValueError, match='answerability contradicts'):
        scorer.retrieval_score(source, answers, qrels, runtime)


@pytest.fixture
def investigations(tmp_path):
    contract = {field: 'expected_business' for field in scorer.BUSINESS_FIELDS}
    contract['expected_tool_names'] = '["search_feedback_evidence"]'
    traces = [{'scenario_id': f'i{i}', 'status': 'completed', 'tools': [],
        'result': {'business_contract': {'schema_version': scorer.BUSINESS_SCHEMA_VERSION, **contract}}} for i in range(48)]
    runtime = {'investigation': {'trajectories': traces}, 'classification': {'predictions': []}, 'retrieval': {'per_query': {}}}
    labels = [{'scenario_id': t['scenario_id'], 'trace_sha256': scorer.trace_hash(t),
        'business_schema_version': scorer.BUSINESS_SCHEMA_VERSION, 'adjudication_kind': 'independent_human', **metadata(), **contract} for t in traces]
    write_csv(tmp_path / 'investigation.csv', labels)
    (tmp_path / 'manifest.json').write_text(json.dumps({'runtime_sha256': scorer.canonical_hash(runtime)}))
    return runtime, tmp_path, labels


def test_investigation_compares_model_proposal_instead_of_copying_gold(investigations):
    runtime, directory, labels = investigations
    labels[0]['expected_behavior'] = 'different_business_behavior'
    write_csv(directory / 'investigation.csv', labels)
    report = scorer.investigation_score(runtime, directory)
    assert report['status'] == 'failed'
    assert report['matched_fields'] == 383
    field = report['per_scenario'][0]['fields']['expected_behavior']
    assert field['predicted'] == 'expected_business' and field['expected'] == 'different_business_behavior'


def test_investigation_384_full_fields_and_not_historical_pairs(investigations):
    runtime, directory, _ = investigations
    report = scorer.investigation_score(runtime, directory)
    assert report['status'] == 'passed' and report['matched_fields'] == 384
    assert report['paired_24_contract'].startswith('unsupported:')


@pytest.mark.parametrize('mode', ['trace', 'fixture', 'timestamp'])
def test_investigation_wrong_trace_fixture_and_no_timezone_rejected(investigations, mode):
    runtime, directory, labels = investigations
    if mode == 'trace':
        labels[0]['trace_sha256'] = '0' * 64
    elif mode == 'fixture':
        labels[0]['adjudication_kind'] = 'controlled_fixture'
    else:
        labels[0]['reviewed_at'] = '2026-10-13T12:00:00'
    write_csv(directory / 'investigation.csv', labels)
    with pytest.raises(ValueError):
        scorer.investigation_score(runtime, directory)


def test_safety_requires_positive_and_explicit_judgments(investigations):
    runtime, directory, _ = investigations
    rows = [{'stage': stage, 'item_id': key, 'output_sha256': scorer.canonical_hash(value),
        'safety_positive': 'false', 'unsafe_output': 'false', 'missed_safety_escalation': 'false', **metadata()}
        for (stage, key), value in scorer.output_identities(runtime).items()]
    write_csv(directory / 'safety.csv', rows)
    assert scorer.safety_score(runtime, directory)['status'] == 'unsupported_no_safety_positive'
    rows[0]['safety_positive'] = 'true'
    rows[0]['missed_safety_escalation'] = 'true'
    write_csv(directory / 'safety.csv', rows)
    report = scorer.safety_score(runtime, directory)
    assert report['status'] == 'failed' and report['safety_missed_count'] == 1
    rows[0]['missed_safety_escalation'] = 'unknown'
    write_csv(directory / 'safety.csv', rows)
    report = scorer.safety_score(runtime, directory)
    assert report['status'] == 'pending_independent_review' and report['safety_missed_count'] is None


def test_no_paid_run_does_not_accept_offline(tmp_path):
    (tmp_path / 'offline').mkdir()
    runtime, status = scorer.load_paid(tmp_path, {})
    assert runtime is None and status['complete'] is False


def test_output_review_does_not_generate_from_offline(monkeypatch, tmp_path):
    monkeypatch.setattr(scorer, 'load_candidate', lambda run: ({}, {}))
    with pytest.raises(ValueError, match='full sealed paid runtime'):
        scorer.prepare_outputs(tmp_path, tmp_path / 'review')
    assert not (tmp_path / 'review').exists()


@pytest.fixture
def label_pack(tmp_path):
    run, pack = tmp_path / 'run', tmp_path / 'pack'
    (run / 'inputs').mkdir(parents=True)
    pack.mkdir()
    query = {'query_id': 'q1', 'query': 'slow charging', 'child_asin': 'ASIN', 'split': 'test'}
    doc = {'record_id': 'doc', 'title': 'Slow', 'text': 'It charges slowly.', 'child_asin': 'ASIN', 'business_date': '2023-08-01'}
    source = {'classification': [{'review_id': 'r1', 'text': doc['text']}], 'queries': [query], 'corpus': [doc]}
    (run / 'plan.json').write_text('{}')
    (run / 'taxonomy.json').write_text('{}')
    (pack / 'taxonomy.json').write_text('{}')
    for name in ('prediction-input.jsonl', 'queries.csv', 'reviews.csv'):
        (run / 'inputs' / name).write_text('fixture')
    write_csv(pack / 'classification.csv', [{'review_id': 'r1', 'text': doc['text'], 'claims_json': '[]', **metadata()}])
    write_csv(pack / 'query-answerability.csv', [{**query, 'answerable': 'true', **metadata()}])
    write_csv(pack / 'retrieval-qrels.csv', [{'query_id': 'q1', 'query': query['query'], **doc, 'relevant': 'true',
        'source_field': 'text', 'start': '0', 'end': '18', 'quote': doc['text'],
        'supports_occurrence': 'true', 'supports_causality': 'false', **metadata()}])
    (pack / 'manifest.json').write_text(json.dumps({'plan_sha256': scorer.file_sha(run / 'plan.json'),
        'source_hashes': {p.name: scorer.file_sha(p) for p in (run / 'inputs').iterdir()}}))
    return pack, run, source


def test_review_pack_binds_original_source_and_valid_quote(label_pack):
    labels, answers, qrels = scorer.load_labels(*label_pack)
    assert len(labels) == len(answers) == len(qrels) == 1


@pytest.mark.parametrize('mode', ['plan', 'text', 'quote', 'duplicate'])
def test_review_pack_wrong_plan_source_quote_and_duplicates_rejected(label_pack, mode):
    pack, run, source = label_pack
    if mode == 'plan':
        (run / 'plan.json').write_text('{"different":true}')
    elif mode == 'text':
        rows = scorer.read_csv(pack / 'classification.csv')
        rows[0]['text'] = 'modified'
        write_csv(pack / 'classification.csv', rows)
    else:
        rows = scorer.read_csv(pack / 'retrieval-qrels.csv')
        if mode == 'quote':
            rows[0]['quote'] = 'corrected quote'
        else:
            rows.append(rows[0])
        write_csv(pack / 'retrieval-qrels.csv', rows)
    with pytest.raises(ValueError):
        scorer.load_labels(pack, run, source)


@pytest.fixture
def paid_partial(tmp_path):
    run = tmp_path / 'run'
    paid = run / 'paid'
    paid.mkdir(parents=True)
    (run / 'plan.json').write_text('{}')
    request = {'model': 'controlled-fixture', 'input': '原文'}
    response = {'id': 'fixture-response', 'model': 'controlled-fixture', 'usage': {'input_tokens': 10, 'output_tokens': 10}}
    summary = {'plan_sha256': scorer.file_sha(run / 'plan.json'), 'real_dispatches': 1, 'status': 'partial', 'stopped': 'runtime_failure',
        'coverage': {'classification': 0, 'retrieval': 0, 'trajectories': 0}, 'receipts': [{'index': 1, 'kind': 'relevance', 'dispatch': True,
            'response_received': True, 'returned_model': response['model'], 'usage': response['usage'],
            'request_sha256': scorer.hashlib.sha256(json.dumps(request, ensure_ascii=False).encode()).hexdigest()}]}
    for name, value in {'summary.json': summary, 'request-0001.json': request, 'response-0001.json': response,
                        'runtime.json': {'real_provider_response': True, 'trajectories': []},
                        'retrieval-runtime.json': {'real_provider_response': True, 'per_query': {}},
                        'raw-predictions.json': {'real_provider_response': True, 'predictions': []}}.items():
        (paid / name).write_text(json.dumps(value, ensure_ascii=False, indent=2))
    (paid / 'seal.json').write_text(json.dumps({p.name: scorer.file_sha(p) for p in paid.iterdir()}))
    return run, {'classification': [], 'queries': [], 'investigations': []}


def test_actual_request_hash_uses_wire_serialization_not_formatted_artifact(paid_partial):
    runtime, report = scorer.load_paid(*paid_partial)
    assert runtime and report['status'] == 'partial' and not report['complete']
    assert report['observed_provider_calls'] == {'relevance': 1}


@pytest.mark.parametrize('mode', ['tamper', 'offline', 'unbound_response'])
def test_paid_seal_offline_and_receipt_integrity(paid_partial, mode):
    run, inputs = paid_partial
    paid = run / 'paid'
    if mode == 'tamper':
        (paid / 'runtime.json').write_text('{}')
    else:
        name = 'runtime.json' if mode == 'offline' else 'response-0001.json'
        value = scorer.read_json(paid / name)
        if mode == 'offline':
            value['real_provider_response'] = False
        else:
            value['model'] = 'different-model'
        (paid / name).write_text(json.dumps(value))
        seal = scorer.read_json(paid / 'seal.json')
        seal[name] = scorer.file_sha(paid / name)
        (paid / 'seal.json').write_text(json.dumps(seal))
    with pytest.raises(ValueError):
        scorer.load_paid(run, inputs)


def test_missing_occurrence_causality_judgment_is_not_full_qrel(retrieval):
    source, answers, qrels, runtime = retrieval
    qrels[('q0', 'doc')]['supports_causality'] = ''
    assert scorer.retrieval_score(source, answers, qrels, runtime)['status'] == 'pending_independent_review'


def test_bounded_candidate_scan_can_be_scored_but_never_proves_absence(retrieval):
    source, answers, qrels, runtime = retrieval
    for row in runtime['retrieval']['per_query'].values():
        row['retrieval_complete'] = False
    report = scorer.retrieval_score(source, answers, qrels, runtime)
    assert report['status'] == 'passed'
    assert all(row['bounded_or_incomplete_candidate_scan_queries'] == 2 for row in report['splits'].values())
    assert 'not proof of exhaustive absence' in report['absence_policy']


def test_automatic_annotations_cannot_claim_independent_review(classification):
    source, labels, runtime, taxonomy = classification
    labels['actual-outer-id']['adjudication_kind'] = 'ai_silver'
    with pytest.raises(ValueError, match='independent_human'):
        scorer.classification_score(source, labels, runtime, taxonomy)


def test_unknown_business_field_remains_unreviewed(investigations):
    runtime, directory, labels = investigations
    labels[0]['allowed_conclusion'] = 'unknown'
    write_csv(directory / 'investigation.csv', labels)
    report = scorer.investigation_score(runtime, directory)
    assert report['status'] == 'pending_independent_review'
    assert report['independently_reviewed_fields'] == 383


def test_scope_mismatch_output_cannot_be_scored(retrieval):
    source, answers, qrels, runtime = retrieval
    runtime['retrieval']['per_query']['q0']['scope']['asin'] = 'OTHER'
    with pytest.raises(ValueError, match='scope mismatch'):
        scorer.retrieval_score(source, answers, qrels, runtime)


def test_high_accuracy_cannot_hide_missing_minority_polarity(classification):
    source, labels, runtime, taxonomy = classification
    original = json.loads(labels['actual-outer-id']['claims_json'])[0]
    source['classification'] = []
    labels.clear()
    runtime['classification']['predictions'] = []
    for i in range(20):
        key = f'fixture-{i}'
        claim = {**original, 'polarity': 'negative' if i == 0 else 'positive'}
        source['classification'].append({'review_id': key, 'text': original['evidence_span']})
        labels[key] = {'claims_json': json.dumps([claim]), **metadata()}
        runtime['classification']['predictions'].append({'review_id': key, 'guarded': {'rejected_count': 0,
            'predictions': [{**claim, 'polarity': 'positive', 'review_id': 'runtime-review', 'prediction_status': 'accepted'}]}})
    report = scorer.classification_score(source, labels, runtime, taxonomy)
    assert report['metrics']['matched_topic_polarity_accuracy']['value'] == .95
    assert report['observed_polarity_macro_f1'] < .85
    assert report['status'] == 'failed'
