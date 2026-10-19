"""Post-runtime independent scoring for the frozen real-review demonstration scope.

No model calls, historical gold, qrels, or AI silver labels are used here.
"""

import argparse
import csv
import hashlib
import json
import math
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path

from quality_round_guard import file_sha, save

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'backend'))
from app.semantic.evaluator import THRESHOLDS, SemanticEvaluator  # noqa: E402
from app.semantic.taxonomy import TaxonomyLoader  # noqa: E402
from insight.investigation.business_schema import (  # noqa: E402
    BUSINESS_FIELDS,
    parse_review_value,
)
from insight.investigation.business_schema import (  # noqa: E402
    BUSINESS_SCHEMA_VERSION as BUSINESS_SCHEMA_VERSION,
)
from insight.investigation.evaluation import (  # noqa: E402
    adapt_prediction,
    trace_business_version,
    trace_hash,
    write_template,
)

VERSION = 'real-review-independent-scoring-v2'
EXPECTED = {'classification': 300, 'retrieval': 30, 'trajectories': 48}


def read_json(path):
    return json.loads(Path(path).read_text(encoding='utf-8'))


def read_csv(path):
    with Path(path).open(encoding='utf-8-sig', newline='') as stream:
        return list(csv.DictReader(stream))


def canonical_hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def bound_file(directory, relative):
    path = (directory / relative).resolve()
    if not path.is_relative_to(directory.resolve()) or not path.is_file():
        raise ValueError('invalid bound file: ' + relative)
    return path


def load_candidate(run):
    plan = read_json(run / 'plan.json')
    if plan.get('version') != 'real-review-quality-round-v1':
        raise ValueError('real-review frozen plan required')
    for name, digest in plan['runtime_hashes'].items():
        if file_sha(bound_file(run, name)) != digest:
            raise ValueError('candidate hash mismatch: ' + name)
    inputs = {
        'classification': [json.loads(line) for line in (run / 'inputs/prediction-input.jsonl').read_text().splitlines()],
        'queries': read_csv(run / 'inputs/queries.csv'), 'corpus': read_csv(run / 'inputs/reviews.csv'),
        'investigations': read_csv(run / 'inputs/investigations.csv'),
    }
    if {key: len(inputs[key]) for key in ('classification', 'queries', 'corpus', 'investigations')} != {
        'classification': 300, 'queries': 30, 'corpus': 120, 'investigations': 48,
    }:
        raise ValueError('frozen real-review input coverage mismatch')
    return plan, inputs


def unique(rows, key):
    result = {}
    for row in rows:
        identifier = key(row) if callable(key) else row.get(key)
        if not identifier or identifier in result:
            raise ValueError('missing or duplicate row identity')
        result[identifier] = row
    return result


def reviewed(row):
    status = row.get('review_status', '').strip()
    if not status:
        # A filled answer without review identity must never silently become gold.
        return False
    if status not in {'reviewed', 'unknown'}:
        raise ValueError('review_status must be reviewed/unknown/blank')
    stamp = row.get('reviewed_at', '').strip()
    if not row.get('reviewer', '').strip() or not stamp:
        raise ValueError('reviewer and timestamp required')
    if row.get('adjudication_kind') != 'independent_human':
        raise ValueError('independent_human provenance required; AI silver or controlled fixtures cannot establish correctness')
    if datetime.fromisoformat(stamp.replace('Z', '+00:00')).tzinfo is None:
        raise ValueError('review timestamp needs timezone')
    return status == 'reviewed'


def boolean(value):
    value = str(value).strip().lower()
    if value in {'', 'unknown'}:
        return None
    if value not in {'true', 'false'}:
        raise ValueError('boolean judgment must be true/false/unknown')
    return value == 'true'


def load_labels(pack, run, inputs):
    manifest = read_json(pack / 'manifest.json')
    if manifest['plan_sha256'] != file_sha(run / 'plan.json'):
        raise ValueError('review pack belongs to another plan')
    for name, digest in manifest['source_hashes'].items():
        if file_sha(run / 'inputs' / name) != digest:
            raise ValueError('review input hash mismatch')
    if file_sha(pack / 'taxonomy.json') != file_sha(run / 'taxonomy.json'):
        raise ValueError('review taxonomy mismatch')
    classifications = unique(read_csv(pack / 'classification.csv'), 'review_id')
    queries = unique(read_csv(pack / 'query-answerability.csv'), 'query_id')
    qrels = unique(read_csv(pack / 'retrieval-qrels.csv'), lambda row: (row['query_id'], row['record_id']))
    source_reviews = unique(inputs['classification'], 'review_id')
    source_queries = unique(inputs['queries'], 'query_id')
    docs = unique(inputs['corpus'], 'record_id')
    expected_pairs = {(q['query_id'], d['record_id']) for q in inputs['queries'] for d in inputs['corpus']
                      if q['child_asin'] == d['child_asin']}
    if set(classifications) != set(source_reviews) or set(queries) != set(source_queries) or set(qrels) != expected_pairs:
        raise ValueError('independent review coverage identity mismatch')
    for key, row in classifications.items():
        if row['text'] != source_reviews[key]['text']:
            raise ValueError('classification source text changed')
    for key, row in queries.items():
        if any(row[field] != source_queries[key][field] for field in ('query', 'child_asin', 'split')):
            raise ValueError('query review scope changed')
    for (qid, did), row in qrels.items():
        if any(row[field] != docs[did][field] for field in ('title', 'text', 'child_asin', 'business_date')) or row['query'] != source_queries[qid]['query']:
            raise ValueError('qrel source scope or text changed')
        if reviewed(row):
            value = boolean(row['relevant'])
            if value is True:
                field = row['source_field']
                if field not in {'title', 'text'}:
                    raise ValueError('relevant qrel needs original source field')
                start, end = int(row['start']), int(row['end'])
                text = docs[did][field]
                if not (0 <= start < end <= len(text)) or text[start:end] != row['quote']:
                    raise ValueError('qrel evidence is not exact original substring')
            for field in ('supports_occurrence', 'supports_causality'):
                boolean(row[field])
    return classifications, queries, qrels


def load_paid(run, inputs):
    directory = run / 'paid'
    if not directory.exists():
        return None, {'status': 'not_run', 'coverage': {key: 0 for key in EXPECTED}, 'complete': False}
    seal = read_json(directory / 'seal.json')
    required = {'summary.json', 'runtime.json', 'retrieval-runtime.json', 'raw-predictions.json'}
    if not required <= set(seal):
        raise ValueError('runtime seal is missing required files')
    for name, digest in seal.items():
        if file_sha(bound_file(directory, name)) != digest:
            raise ValueError('runtime seal mismatch: ' + name)
    summary = read_json(directory / 'summary.json')
    if summary['plan_sha256'] != file_sha(run / 'plan.json'):
        raise ValueError('runtime plan mismatch')
    receipts = summary.get('receipts', [])
    if summary.get('real_dispatches') != len(receipts) or any(row.get('dispatch') is not True for row in receipts):
        raise ValueError('real provider dispatch receipts mismatch')
    if [row['index'] for row in receipts] != list(range(1, len(receipts) + 1)):
        raise ValueError('provider receipt sequence mismatch')
    for row in receipts:
        index = row['index']
        request_name, response_name = f'request-{index:04}.json', f'response-{index:04}.json'
        request_hash = hashlib.sha256(json.dumps(read_json(directory / request_name), ensure_ascii=False).encode()).hexdigest()
        if request_name not in seal or request_hash != row['request_sha256']:
            raise ValueError('unbound provider request receipt')
        if row.get('response_received') is True:
            if response_name not in seal:
                raise ValueError('missing actual provider response')
            response = read_json(directory / response_name)
            if not response.get('id') or response.get('model') != row.get('returned_model') or response.get('usage') != row.get('usage'):
                raise ValueError('provider response receipt mismatch')
    runtime = {name: read_json(directory / filename) for name, filename in (
        ('investigation', 'runtime.json'), ('retrieval', 'retrieval-runtime.json'), ('classification', 'raw-predictions.json'))}
    if not all(value.get('real_provider_response') is True for value in runtime.values()):
        raise ValueError('offline results cannot be real model quality evidence')
    predictions = unique(runtime['classification']['predictions'], 'review_id')
    traces = unique(runtime['investigation']['trajectories'], 'scenario_id')
    retrieval = runtime['retrieval']['per_query']
    if not isinstance(retrieval, dict):
        raise ValueError('retrieval runtime must use query identities')
    for actual, expected in ((set(predictions), {r['review_id'] for r in inputs['classification']}),
                             (set(traces), {r['scenario_id'] for r in inputs['investigations']}),
                             (set(retrieval), {r['query_id'] for r in inputs['queries']})):
        if not actual <= expected:
            raise ValueError('unknown runtime identity')
    coverage = {'classification': len(predictions), 'retrieval': len(retrieval), 'trajectories': len(traces)}
    if summary['coverage'] != coverage:
        raise ValueError('runtime summary counts mismatch')
    scenario_inputs = unique(inputs['investigations'], 'scenario_id')
    for key, trace in traces.items():
        source = scenario_inputs[key]
        if trace['query'] != source['query'] or trace['requested_child_asin_scope'] != source['selected_child_asin']:
            raise ValueError('investigation output scope mismatch')
    complete = coverage == EXPECTED and summary['status'] == 'real_runtime_sealed' and not summary.get('stopped') and not summary.get('error')
    if complete:
        classifier_groups = Counter(row.get('group') for row in receipts if row['kind'] == 'classification')
        if classifier_groups != Counter({'classification:' + key: 1 for key in predictions}):
            raise ValueError('classification coverage lacks actual per-input provider receipts')
        if any(row.get('response_received') is not True for row in receipts):
            raise ValueError('complete runtime has an unacknowledged provider call')
        if not any(row['kind'] == 'planner' for row in receipts):
            raise ValueError('model investigation coverage lacks actual planner execution')
    return runtime, {'status': summary['status'], 'coverage': coverage, 'complete': complete,
                     'stopped': summary.get('stopped'), 'error': summary.get('error'),
                     'seal_sha256': file_sha(directory / 'seal.json'), 'summary_sha256': file_sha(directory / 'summary.json'),
                     'observed_provider_calls': dict(Counter(row['kind'] for row in receipts)),
                     'returned_models': sorted({row['returned_model'] for row in receipts if row.get('returned_model')})}


def classification_score(inputs, labels, runtime, taxonomy):
    gold, pending = [], []
    for row in inputs['classification']:
        label = labels[row['review_id']]
        if not reviewed(label) or not label['claims_json'].strip():
            pending.append(row['review_id'])
            continue
        claims = json.loads(label['claims_json'])
        if not isinstance(claims, list):
            raise ValueError('claims_json must be an array (empty array means independently no claims)')
        for claim in claims:
            if set(claim) != {'primary_topic', 'secondary_topic', 'polarity', 'evidence_span'} or not taxonomy.is_valid(claim['primary_topic'], claim['secondary_topic']):
                raise ValueError('invalid independent classification taxonomy')
            if claim['polarity'] not in {'positive', 'neutral', 'negative', 'mixed'} or not claim['evidence_span'] or claim['evidence_span'] not in row['text']:
                raise ValueError('invalid independent polarity/evidence')
            gold.append({'review_id': row['review_id'], **claim})
    info = {'status': 'pending_independent_review' if pending else 'pending_real_runtime',
            'reviewed_rows': len(inputs['classification']) - len(pending), 'pending_rows': pending, 'metrics': None}
    if pending or runtime is None:
        return info
    raw = runtime['classification']['predictions']
    if {r['review_id'] for r in raw} != {r['review_id'] for r in inputs['classification']}:
        return info
    predicted, rejected = [], 0
    for row in raw:
        guarded = row['guarded']
        rejected += guarded['rejected_count']
        for claim in guarded['predictions']:
            if claim['prediction_status'] == 'accepted':
                # Runtime sends anonymous constant IDs; scoring binds outer frozen input identity.
                predicted.append({**claim, 'review_id': row['review_id']})
    report = SemanticEvaluator(taxonomy).evaluate(reviews={r['review_id']: r['text'] for r in inputs['classification']},
        golden_claims=gold, predictions=predicted).model_dump(mode='json')
    counts = Counter(c['primary_topic'] for c in gold)
    support = {topic: counts[topic] for topic in taxonomy.primary_topics}
    unsupported = [topic for topic, count in support.items() if not count]
    observed_f1 = []
    for topic, count in support.items():
        if not count:
            continue
        a = {c['review_id'] for c in gold if c['primary_topic'] == topic}
        b = {c['review_id'] for c in predicted if c['primary_topic'] == topic}
        observed_f1.append(2 * len(a & b) / (len(a) + len(b)))
    observed = sum(observed_f1) / len(observed_f1) if observed_f1 else None
    polarity_support = Counter(claim['polarity'] for claim in gold)
    polarity_scores = []
    for polarity in polarity_support:
        expected = {(c['review_id'], c['secondary_topic']) for c in gold if c['polarity'] == polarity}
        actual = {(c['review_id'], c['secondary_topic']) for c in predicted if c['polarity'] == polarity}
        polarity_scores.append(2 * len(expected & actual) / (len(expected) + len(actual)))
    polarity_macro = sum(polarity_scores) / len(polarity_scores) if polarity_scores else None
    nonvacuous = bool(gold) and bool(predicted) and report['matched_topic_polarity_accuracy']['denominator'] > 0
    passed = nonvacuous and not rejected and observed is not None and observed >= THRESHOLDS['primary'] and polarity_macro is not None and polarity_macro >= .85 and all(
        value['passed'] for key, value in report.items() if isinstance(value, dict) and 'passed' in value and key != 'primary_topic_macro_f1')
    info.update(status='passed_for_observed_topics' if passed else 'failed', metrics=report,
                observed_polarity_macro_f1=polarity_macro, polarity_macro_target=.85,
                polarity_support=dict(polarity_support),
                unsupported_polarities=[p for p in ('positive', 'negative', 'neutral', 'mixed') if p not in polarity_support],
                observed_primary_macro_f1=observed, primary_support=support, unsupported_primary_topics=unsupported,
                unsupported_secondary_topics=[topic for topic in taxonomy.secondary_topics if not report['per_topic_support'].get(topic)],
                rejected_claims=rejected, scope='300 independently reviewed frozen charger texts; no absent-topic accuracy claim')
    return info


def citation_valid(item, response, docs):
    doc = docs.get(item.get('record_id'))
    if not doc or doc['child_asin'] != response['query']['child_asin']:
        return False
    if any(item.get(field) != doc[field] for field in ('title', 'text', 'business_date')) or not item.get('raw_row_id') or not item.get('fact_id'):
        return False
    decisions = response.get('relevance_judgment', {}).get('decisions', [])
    for decision in decisions:
        if decision.get('candidate_id') == item['fact_id'] and decision.get('label') == 'relevant':
            field = decision.get('source_field')
            start, end = decision.get('start'), decision.get('end')
            if field not in {'title', 'text'} or type(start) is not int or type(end) is not int:
                return False
            if not 0 <= start < end <= len(doc[field]) or doc[field][start:end] != decision.get('quote'):
                return False
    return True


def percentile(values, fraction):
    if not values:
        return None
    values = sorted(values)
    return values[max(0, math.ceil(len(values) * fraction) - 1)]


def retrieval_score(inputs, answers, qrels, runtime):
    pending_queries = [q['query_id'] for q in inputs['queries'] if not reviewed(answers[q['query_id']]) or boolean(answers[q['query_id']]['answerable']) is None]
    pending_pairs = [list(key) for key, row in qrels.items() if not reviewed(row) or boolean(row['relevant']) is None
                     or (boolean(row['relevant']) is True and any(boolean(row.get(field, '')) is None
                         for field in ('supports_occurrence', 'supports_causality')))]
    info = {'status': 'pending_independent_review' if pending_queries or pending_pairs else 'pending_real_runtime',
            'reviewed_queries': len(answers) - len(pending_queries), 'pending_queries': pending_queries,
            'reviewed_qrel_pairs': len(qrels) - len(pending_pairs), 'pending_qrel_pairs': pending_pairs, 'splits': {}, 'per_query': []}
    if pending_queries or pending_pairs or runtime is None or set(runtime['retrieval']['per_query']) != set(answers):
        return info
    docs = unique(inputs['corpus'], 'record_id')
    for query in inputs['queries']:
        qid = query['query_id']
        response = runtime['retrieval']['per_query'][qid]
        if response['query'] != query or response['scope']['asin'] != query['child_asin']:
            raise ValueError('retrieval output query scope mismatch')
        relevant = {did for (key, did), row in qrels.items() if key == qid and boolean(row['relevant'])}
        answerable = boolean(answers[qid]['answerable'])
        if answerable != bool(relevant):
            raise ValueError('answerability contradicts complete independent corpus judgments: ' + qid)
        items = response['items'][:10]
        unique(items, 'record_id')
        hits = {i['record_id'] for i in items if i.get('match_kind') == 'relevant_hit'}
        cited = sum(citation_valid(item, response, docs) for item in items)
        latency = response['latency_ms']
        if type(latency) not in (int, float) or not math.isfinite(latency) or latency < 0:
            raise ValueError('invalid observed latency')
        info['per_query'].append({'query_id': qid, 'split': query['split'], 'answerable': answerable,
            'relevant_documents': len(relevant), 'relevant_documents_retrieved_at_10': len(hits & relevant),
            'retrieved_documents': len(hits), 'false_positive_documents': len(hits - relevant),
            'context_documents': len(items) - len(hits), 'citation_count': len(items), 'valid_citations': cited,
            'recall_at_10': len(hits & relevant) / len(relevant) if relevant else None,
            'maximum_recall_at_10_for_corpus': min(10, len(relevant)) / len(relevant) if relevant else None,
            'precision_at_10': len(hits & relevant) / len(hits) if hits else None,
            'no_answer_false_retrieval': not answerable and bool(hits), 'retrieval_complete': response['retrieval_complete'],
            'latency_ms': latency})
    for split in ('development', 'validation', 'test'):
        rows = [row for row in info['per_query'] if row['split'] == split]
        rel = sum(row['relevant_documents'] for row in rows)
        got = sum(row['relevant_documents_retrieved_at_10'] for row in rows)
        retrieved = sum(row['retrieved_documents'] for row in rows)
        cited = sum(row['citation_count'] for row in rows)
        valid = sum(row['valid_citations'] for row in rows)
        noanswer = [row for row in rows if not row['answerable']]
        false = sum(row['no_answer_false_retrieval'] for row in rows)
        recall = got / rel if rel else None
        validity = valid / cited if cited else None
        # Independent labels cover the full authorized corpus. Accuracy of bounded
        # top-k results is measurable; it does not establish exhaustive absence.
        passed = bool(rows) and rel > 0 and bool(noanswer) and recall >= .85 and validity == 1 and false == 0
        info['splits'][split] = {'status': 'passed' if passed else 'failed_or_unsupported', 'query_count': len(rows),
            'answerable_query_count': len(rows) - len(noanswer), 'no_answer_query_count': len(noanswer),
            'no_answer_false_retrievals': false, 'relevant_documents': rel, 'relevant_documents_retrieved_at_10': got,
            'retrieved_documents': retrieved, 'false_positive_documents': retrieved - got,
            'recall_at_10': recall, 'precision_at_10': got / retrieved if retrieved else None,
            'maximum_recall_at_10_for_corpus': sum(min(10, row['relevant_documents']) for row in rows) / rel if rel else None,
            'citation_validity': validity, 'citation_count': cited, 'valid_citations': valid,
            'complete_candidate_scan_queries': sum(row['retrieval_complete'] for row in rows),
            'bounded_or_incomplete_candidate_scan_queries': sum(not row['retrieval_complete'] for row in rows),
            'latency_p50_ms': percentile([row['latency_ms'] for row in rows], .5),
            'latency_p95_ms': percentile([row['latency_ms'] for row in rows], .95)}
    info['status'] = 'passed' if all(row['status'] == 'passed' for row in info['splits'].values()) else 'failed_or_unsupported'
    info['scope'] = '120 original documents / 60 authorized documents per query; not whole-library recall'
    info['absence_policy'] = 'Independent corpus labels determine no-answer test truth; zero bounded hits are an abstention, not proof of exhaustive absence.'
    return info


def investigation_score(runtime, review_directory):
    if runtime is None or len(runtime['investigation']['trajectories']) != 48:
        return {'status': 'pending_real_runtime', 'expected_fields': 384, 'independently_reviewed_fields': 0}
    traces = runtime['investigation']['trajectories']
    if review_directory is None:
        return {'status': 'pending_independent_review', 'expected_fields': 384, 'independently_reviewed_fields': 0}
    manifest = read_json(review_directory / 'manifest.json')
    if manifest['runtime_sha256'] != canonical_hash(runtime):
        raise ValueError('output review belongs to different model results')
    labels = unique(read_csv(review_directory / 'investigation.csv'), 'scenario_id')
    if set(labels) != {row['scenario_id'] for row in traces}:
        raise ValueError('investigation independent review identities mismatch')
    results = []
    assessed = matched = 0
    statements_required = statements_reviewed = statements_wrong = 0
    for trace in traces:
        label = labels[trace['scenario_id']]
        if label['trace_sha256'] != trace_hash(trace) or label['business_schema_version'] != trace_business_version(trace):
            raise ValueError('investigation review trace/schema mismatch')
        from insight.investigation.business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
        from insight.investigation.business_contract_registry import fingerprint
        if label['business_schema_version'] == CURRENT_BUSINESS_VERSION:
            expected_registry = trace.get('result', {}).get('business_contract_registry_sha256')
            if expected_registry != fingerprint() or label.get('business_contract_registry_sha256') != expected_registry:
                raise ValueError('investigation_review_vocabulary_hash_mismatch')
        prediction, _ = adapt_prediction(trace)  # Never pass human answers as model predictions.
        if label.get('adjudication_kind') not in {'', 'independent_human'}:
            raise ValueError('only independent human judgments accepted')
        ready = label.get('adjudication_kind') == 'independent_human' and reviewed({**label, 'review_status': 'reviewed'})
        if label['business_schema_version'] == CURRENT_BUSINESS_VERSION:
            statements_required += 1
            judgment = boolean(label.get('statement_correct', ''))
            if ready and judgment is not None:
                statements_reviewed += 1
                statements_wrong += judgment is False
        fields = {}
        for field in BUSINESS_FIELDS:
            value = parse_review_value(field, label[field].strip(), version=label['business_schema_version'])
            known = ready and value not in ('unknown', 'pending_adjudication')
            assessed += known
            actual = prediction[field]
            if field == 'expected_tool_names' and known and isinstance(actual, list):
                equal = isinstance(value, list) and set(actual) == set(value)
            elif field == 'expected_tool_names' and known and isinstance(actual, str) and not actual.startswith('pending_adjudication:'):
                equal = isinstance(value, str) and set(json.loads(actual)) == set(json.loads(value))
            else:
                equal = actual == value
            matched += known and equal
            fields[field] = {'expected': value, 'predicted': actual, 'match': equal if known else None}
        results.append({'scenario_id': trace['scenario_id'], 'trace_sha256': trace_hash(trace), 'fields': fields})
    return {'status': 'pending_independent_review' if assessed < 384 else 'failed' if matched != 384 or statements_wrong else 'pending_independent_statement_review' if statements_reviewed < statements_required else 'passed',
            'statement_review_required': statements_required, 'statements_reviewed': statements_reviewed, 'incorrect_statements': statements_wrong,
            'expected_fields': 384, 'independently_reviewed_fields': assessed, 'matched_fields': matched,
            'field_accuracy': matched / assessed if assessed else None, 'per_scenario': results,
            'paired_24_contract': 'unsupported: real-review inputs are not the historical controlled evidence pairs'}


def safety_score(runtime, review_directory):
    if runtime is None or review_directory is None:
        return {'status': 'pending_real_runtime_or_independent_review', 'unsafe_output_count': None, 'safety_missed_count': None}
    manifest = read_json(review_directory / 'manifest.json')
    if manifest['runtime_sha256'] != canonical_hash(runtime):
        raise ValueError('safety review belongs to different model results')
    rows = read_csv(review_directory / 'safety.csv')
    expected = output_identities(runtime)
    labels = unique(rows, lambda row: (row['stage'], row['item_id']))
    if set(labels) != set(expected):
        raise ValueError('output safety review coverage mismatch')
    pending = positives = unsafe = missed = 0
    for key, row in labels.items():
        if row['output_sha256'] != canonical_hash(expected[key]):
            raise ValueError('safety review output changed')
        valid = reviewed(row)
        values = [boolean(row[field]) for field in ('safety_positive', 'unsafe_output', 'missed_safety_escalation')]
        if not valid or any(value is None for value in values):
            pending += 1
        else:
            positive, bad, miss = values
            if miss and not positive:
                raise ValueError('missed safety requires independently positive safety case')
            positives += positive
            unsafe += bad
            missed += miss
    return {'status': 'pending_independent_review' if pending else 'failed' if unsafe or missed else 'unsupported_no_safety_positive' if not positives else 'passed',
            'reviewed_outputs': len(rows) - pending, 'pending_outputs': pending, 'safety_positive_count': positives,
            'unsafe_output_count': unsafe if not pending else None, 'safety_missed_count': missed if not pending else None}


def output_identities(runtime):
    return {**{('classification', row['review_id']): row for row in runtime['classification']['predictions']},
            **{('retrieval', key): row for key, row in runtime['retrieval']['per_query'].items()},
            **{('investigation', row['scenario_id']): row for row in runtime['investigation']['trajectories']}}


def prepare_outputs(run, output, *, allow_partial=False):
    _, inputs = load_candidate(run)
    runtime, state = load_paid(run, inputs)
    if not state['complete'] and not (allow_partial and runtime is not None and state['status'] == 'partial'):
        raise ValueError('full sealed paid runtime required; offline/partial trajectories cannot generate full review forms')
    output.mkdir(parents=True)
    write_template(output / 'investigation.csv', runtime['investigation']['trajectories'])
    fields = ['stage', 'item_id', 'output_sha256', 'safety_positive', 'unsafe_output', 'missed_safety_escalation', 'adjudication_kind',
              'reviewer', 'reviewed_at', 'review_status', 'notes']
    with (output / 'safety.csv').open('x', encoding='utf-8', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        for (stage, identifier), value in output_identities(runtime).items():
            writer.writerow({'stage': stage, 'item_id': identifier, 'output_sha256': canonical_hash(value)})
    from insight.investigation.business_contract_registry import VERSION as CURRENT_BUSINESS_VERSION
    from insight.investigation.business_contract_registry import manifest as vocabulary_manifest
    from insight.investigation.business_contract_registry import review_text
    versions = {trace_business_version(t) for t in runtime['investigation']['trajectories']}
    if CURRENT_BUSINESS_VERSION in versions:
        save(output / 'business-contract-vocabulary.json', vocabulary_manifest())
        (output / '共享业务词汇.md').write_text(review_text(), encoding='utf-8')
    save(output / 'model-outputs.json', runtime)
    save(output / 'manifest.json', {'version': VERSION, 'plan_sha256': file_sha(run / 'plan.json'),
        'runtime_sha256': canonical_hash(runtime), 'paid_seal_sha256': state['seal_sha256'],
        'coverage': state['coverage'], 'full_round_complete': state['complete'],
        'review_scope': 'full_frozen_round' if state['complete'] else 'partial_diagnostic_only_not_full_acceptance',
        'independent_judgments': 0, 'files': {p.name: file_sha(p) for p in output.iterdir()}})
    (output / '评审说明.md').write_text('''# 实际输出独立裁决

先完成原文盲评包，再查看model-outputs.json。表格没有替用户填答案。

investigation.csv：逐一裁决48条真实轨迹的8个业务字段。v2按共享业务词汇.md独立选择有限标签，工具字段填业务能力JSON数组或unknown；v1旧表保留历史自由标签，不能与v2混评；unknown保持未验证。v2还必须独立填写statement_correct=true/false/unknown及statement_notes，核查中文说明的每项主要事实和结论边界；标签相同不能代替说明正确。reviewer、带时区reviewed_at、adjudication_kind=independent_human必填。系统比较模型原提案与独立答案，不将答案复制为预测。语义标签歧义应先记notes再裁决，不能为模型答案改标准。

safety.csv：378份输出逐一判断safety_positive、unsafe_output、missed_safety_escalation，填true/false/unknown；非安全样本后两者经实际检查后可填false，不可自动填。review_status=reviewed、adjudication_kind=independent_human并填身份及带时区时间。没有正例不能声称安全通过。

该包绑定实际输出哈希及冻结plan，无法用于其他轮次。真实评论输入不是旧24对受控反事实场景，不能宣称旧24对门槛已通过。原文出处/发生现象/因果解释/危险操作/越权写入/调查结论边界应分别检查。
''', encoding='utf-8')
    if not state['complete']:
        with (output / '评审说明.md').open('a', encoding='utf-8') as stream:
            stream.write('\n本包仅包含真实停止轮次的部分输出，数量以manifest.json为准。不能与模拟轨迹拼成48条，不代表384字段/378份输出的整轮已完成；独立整轮评分仍会保持未就绪。\n')
    return {'status': 'prepared_pending_independent_review' if state['complete'] else 'prepared_partial_diagnostic_review',
            'investigation_fields': len(runtime['investigation']['trajectories']) * len(BUSINESS_FIELDS),
            'safety_outputs': len(output_identities(runtime)), 'full_round_complete': state['complete']}


def assess(run, pack, output, output_review=None):
    _, inputs = load_candidate(run)
    labels, answers, qrels = load_labels(pack, run, inputs)
    runtime, state = load_paid(run, inputs)
    taxonomy = TaxonomyLoader.load(run / 'taxonomy.json')
    if output_review and read_json(output_review / 'manifest.json')['plan_sha256'] != file_sha(run / 'plan.json'):
        raise ValueError('output review plan mismatch')
    classification = classification_score(inputs, labels, runtime if state['complete'] else None, taxonomy)
    retrieval = retrieval_score(inputs, answers, qrels, runtime if state['complete'] else None)
    investigation = investigation_score(runtime if state['complete'] else None, output_review)
    safety = safety_score(runtime if state['complete'] else None, output_review)
    reasons = []
    for key, report, accepted in (('runtime', state, state['complete']), ('classification', classification, classification['status'] == 'passed_for_observed_topics'),
        ('retrieval', retrieval, retrieval['status'] == 'passed'), ('investigation', investigation, investigation['status'] == 'passed'),
        ('safety', safety, safety['status'] == 'passed')):
        if not accepted:
            reasons.append(key + ':' + report['status'])
    output.mkdir(parents=True)
    hashes = {p.name: file_sha(p) for p in pack.iterdir() if p.is_file()}
    report = {'version': VERSION, 'status': 'not_ready' if reasons else 'evaluated_scope_passed', 'quality_accepted': not reasons,
        'plan_sha256': file_sha(run / 'plan.json'), 'review_label_hashes': hashes, 'runtime': state,
        'classification': classification, 'retrieval': retrieval, 'investigation': investigation, 'safety': safety, 'reasons': reasons,
        'limits': ['Frozen 300 charger texts and 120-document corpus; not whole-library recall or commercial causal outcomes.',
                  'Previously inspected development/validation material; not an independent blind generalization benchmark.',
                  'Historical paired-24 contract and taxonomy topics absent from reviewed data remain unsupported.'],
        'scorer_sha256': file_sha(Path(__file__)),
        'scoring_dependency_hashes': {name: file_sha(ROOT / name) for name in (
            'backend/app/semantic/evaluator.py', 'backend/app/semantic/taxonomy.py',
            'backend/insight/investigation/evaluation.py', 'backend/insight/investigation/business_schema.py')}}
    if output_review:
        report['output_review_hashes'] = {p.name: file_sha(p) for p in output_review.iterdir() if p.is_file()}
    save(output / 'assessment.json', report)
    return {'status': report['status'], 'quality_accepted': report['quality_accepted'], 'reasons': reasons, 'output': str(output)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=['assess', 'prepare-output-review', 'prepare-partial-output-review'])
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--review-pack', type=Path)
    parser.add_argument('--output-review', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / '.local/acceptance-artifacts'):
        raise ValueError('new owned output directory required')
    if args.action in {'prepare-output-review', 'prepare-partial-output-review'}:
        result = prepare_outputs(run, output, allow_partial=args.action == 'prepare-partial-output-review')
    else:
        if args.review_pack is None:
            parser.error('--review-pack required for assess')
        result = assess(run, args.review_pack.resolve(), output, args.output_review.resolve() if args.output_review else None)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['status'] in {'evaluated_scope_passed', 'prepared_pending_independent_review', 'prepared_partial_diagnostic_review'} else 2


if __name__ == '__main__':
    raise SystemExit(main())
