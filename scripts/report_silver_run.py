"""Read-only silver receipts audit/export. No provider initialization or retries."""
import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

from ai_silver_label import digest, file_sha, load_rows, same_label, validate_label


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def audit(run, input_path, manifest_path, output):
    if output.exists():
        raise ValueError('unique reporting directory required')
    rows = load_rows(input_path, manifest_path)
    by_id = {row['review_id']: row for row in rows}
    binding = json.loads((run / 'input-binding.json').read_text())
    config = json.loads((run / 'approved-config.json').read_text())
    assert file_sha(input_path) == binding['input_sha256'] == config['input_sha256']
    assert file_sha(manifest_path) == binding['manifest_sha256'] == config['manifest_sha256']
    assert file_sha(run / 'approved-config.json') == binding['config_sha256']
    assert file_sha(run / 'authorization.json') == binding['authorization_sha256']
    runtime = run / 'runtime'
    calls = [json.loads(p.read_text()) for p in sorted(runtime.glob('call-*.json'))]
    assert [r['call_id'] for r in calls] == list(range(1, len(calls) + 1))
    assert len(calls) <= 900
    groups = defaultdict(dict)
    reserved = Decimal(0)
    estimated = Decimal(0)
    configured_upper_usage = Decimal(0)
    unknown_reserved = Decimal(0)
    token_totals = Counter()
    validation_failures = []
    seen_responses = set()
    cost_rows = []
    for call in calls:
        row = by_id[call['review_id']]
        assert call['input_sha256'] == digest({'text': row['text']})
        assert call['label_source'] == 'ai_generated/silver'
        assert call['provider'] == 'deepseek' and call['requested_model'] == 'deepseek-flash'
        phase = call['phase']
        assert phase in {'first_1', 'first_2', 'disagreement_review'} and phase not in groups[row['review_id']]
        groups[row['review_id']][phase] = call
        if phase.startswith('first_'):
            assert json.loads(call['messages'][1]['content']) == {'text': row['text']}
        assert call['prompt_sha256'] == digest({'messages': call['messages'], 'schema': call['schema']})
        allowance = Decimal(call['reserved_upper_usd'])
        assert allowance <= Decimal(config['max_call_usd'])
        reserved += allowance
        response_id = call.get('provider_response_id')
        if response_id:
            assert response_id not in seen_responses
            seen_responses.add(response_id)
        if call.get('label'):
            validate_label(call['label'], row['text'])
        if call['status'] != 'validated_silver':
            validation_failures.append({'review_id': row['review_id'], 'split': row['split'], 'call_id': call['call_id'], 'phase': phase,
                                        'status': call['status'], 'error_type': call.get('error_type'), 'http_status': call.get('http_status'),
                                        'reason': call.get('validation_error') or call.get('error_type') or 'dispatch receipt incomplete',
                                        'followup': 'retain original receipt; no automatic retry. New repair/labeling work requires bounded authorization; do not tune from holdout answers.'})
        usage = call.get('usage') or {}
        i, o = usage.get('prompt_tokens'), usage.get('completion_tokens')
        known = type(i) is int and type(o) is int and min(i, o) >= 0
        quote_cost = None
        scope = 'unknown_no_usage'
        if known:
            token_totals['prompt_tokens'] += i
            token_totals['completion_tokens'] += o
            token_totals['total_tokens'] += i + o
            configured = (Decimal(i) * Decimal('.30') + Decimal(o) * Decimal('1.20')) / 1000000
            assert Decimal(call['actual_cost_usd']) == configured <= allowance
            configured_upper_usage += configured
            hit = usage.get('prompt_cache_hit_tokens')
            miss = usage.get('prompt_cache_miss_tokens')
            complete_cache = type(hit) is int and type(miss) is int and min(hit, miss) >= 0 and hit + miss == i
            if not complete_cache:
                hit, miss = 0, i
            token_totals['cache_hit_tokens_reported_or_zero'] += hit
            token_totals['cache_miss_tokens_reported_or_upper'] += miss
            date = datetime.fromisoformat(call['started_at_utc'])
            # This run is on Saturday: officially off-peak all day, no holiday inference.
            weekend = date.weekday() >= 5
            peak = not weekend and (1 <= date.hour < 4 or 6 <= date.hour < 10)
            rates = (Decimal('.006'), Decimal('.30'), Decimal('1.20')) if peak else (Decimal('.003'), Decimal('.15'), Decimal('.60'))
            quote_cost = (Decimal(hit) * rates[0] + Decimal(miss) * rates[1] + Decimal(o) * rates[2]) / 1000000
            estimated += quote_cost
            scope = 'offpeak_weekend_quote_with_reported_cache' if weekend and complete_cache else 'quote_upper_cache_or_holiday_unverified'
        else:
            unknown_reserved += allowance
        cost_rows.append({'call_id': call['call_id'], 'review_id': row['review_id'], 'phase': phase,
                          'started_at_utc': call.get('started_at_utc'), 'reported_model': call.get('reported_model'),
                          'provider_response_id': response_id, 'input_tokens': i, 'output_tokens': o,
                          'latency_ms': call.get('latency_ms'), 'reserved_upper_usd': str(allowance),
                          'configured_peak_usage_upper_usd': call.get('actual_cost_usd'),
                          'usage_quote_estimate_usd': str(quote_cost) if quote_cost is not None else None, 'quote_scope': scope,
                          'supplier_invoice_verified': False})
    assert reserved <= Decimal(config['max_total_usd'])
    ledger = json.loads((runtime / 'budget-ledger.json').read_text())
    assert Decimal(ledger['reserved_upper_usd']) == reserved and ledger['dispatch_allowances_consumed'] == len(calls)
    final = json.loads((runtime / 'silver-labels.json').read_text()) if (runtime / 'silver-labels.json').exists() else None
    delivered = {row['review_id']: row for row in final['labels']} if final else {}
    if final:
        assert len(delivered) == len(final['labels']) == len(rows)
    labels = []
    disagreements = []
    for row in rows:
        passes = groups[row['review_id']]
        first = [passes.get(name) for name in ['first_1', 'first_2']]
        first_labels = [r.get('label') if r else None for r in first]
        agreement = same_label(*first_labels)
        review = passes.get('disagreement_review')
        selected = first_labels[0] if agreement else review.get('label') if review else None
        status = 'silver_agreement' if agreement else 'silver_reviewed' if selected and selected['status'] == 'labeled' else 'unresolved'
        if delivered:
            d = delivered[row['review_id']]
            assert d['input_sha256'] == digest({'text': row['text']}) and d['split'] == row['split'] and d['duplicate_group'] == row['duplicate_group']
            assert d['label'] == selected and d['status'] == status and d['first_agreement'] == agreement
        if all(r and r.get('label') for r in first):
            cause = 'label_agreement' if agreement else 'valid_label_difference_or_uncertainty'
        elif all(first):
            cause = 'first_pass_invalid_or_failed'
        else:
            cause = 'not_attempted_after_stop'
        if not agreement:
            disagreements.append({'review_id': row['review_id'], 'split': row['split'], 'cause': cause,
                                  'first_call_ids': [r['call_id'] if r else None for r in first], 'first_labels': first_labels,
                                  'review_call_id': review['call_id'] if review else None, 'review_label': review.get('label') if review else None,
                                  'final_status': status})
        labels.append({'review_id': row['review_id'], 'split': row['split'], 'duplicate_group': row['duplicate_group'],
                       'text': row['text'], 'input_sha256': digest({'text': row['text']}), 'label_source': 'ai_generated/silver',
                       'status': status, 'label': selected, 'first_agreement': agreement, 'cause': cause,
                       'first_call_ids': [r['call_id'] if r else None for r in first], 'review_call_id': review['call_id'] if review else None})
    raw_proposal_agreements = 0
    raw_proposal_differences = 0
    raw_unparseable_pairs = 0
    for row in rows:
        pair = groups[row['review_id']]
        proposals = []
        for phase in ['first_1', 'first_2']:
            try:
                value = json.loads(pair[phase]['raw_response']['choices'][0]['message']['content'])
                assert isinstance(value, dict) and value.get('status') in {'labeled', 'uncertain', 'abstain'} and isinstance(value.get('topics'), list)
                proposals.append(value)
            except (KeyError, IndexError, ValueError, TypeError, AssertionError):
                proposals.append(None)
        if any(value is None for value in proposals):
            raw_unparseable_pairs += 1
        elif same_label(*proposals):
            raw_proposal_agreements += 1
        else:
            raw_proposal_differences += 1
    split_summary = {}
    for split in ['development', 'validation', 'holdout']:
        part = [row for row in labels if row['split'] == split]
        split_summary[split] = {'rows': len(part), 'statuses': dict(Counter(r['status'] for r in part)),
                                'review_triggers': dict(Counter(r['cause'] for r in part if not r['first_agreement']))}
    result = {'status': 'completed_silver_not_human_gold' if final else 'stopped_partial_silver', 'rows': len(rows),
              'attempted_first_calls': sum(r['phase'].startswith('first_') for r in calls),
              'review_calls': sum(r['phase'] == 'disagreement_review' for r in calls), 'total_calls': len(calls),
              'response_receipts': len(seen_responses), 'receipt_statuses': dict(Counter(r['status'] for r in calls)),
              'final_statuses': dict(Counter(r['status'] for r in labels)),
              'raw_unvalidated_first_proposals': {'agreements': raw_proposal_agreements, 'differences_or_uncertainty': raw_proposal_differences, 'unparseable_or_missing_pairs': raw_unparseable_pairs, 'note': 'ignores span validity; not accepted labels or accuracy'}, 'review_triggers': dict(Counter(r['cause'] for r in labels if not r['first_agreement'])),
              'splits': split_summary, 'tokens': dict(token_totals),
              'billing': {'usage_quote_estimate_known_calls_usd': str(estimated), 'unknown_charge_reserved_upper_usd': str(unknown_reserved),
                          'unknown_charge_calls': sum(r['usage_quote_estimate_usd'] is None for r in cost_rows),
                          'configured_peak_usage_upper_known_calls_usd': str(configured_upper_usage),
                          'reserved_upper_total_usd': str(reserved), 'approved_total_hard_cap_usd': config['max_total_usd'],
                          'remaining_unreserved_budget_usd': str(Decimal(config['max_total_usd']) - reserved),
                          'usage_quote_estimate_budget_balance_usd': str(Decimal(config['max_total_usd']) - estimated) if not unknown_reserved else None,
                          'supplier_invoice_verified': False, 'supplier_invoice_paid_amount_usd': None,
                          'note': 'runtime actual_cost_usd is configured peak/no-cache conservative usage accounting, not supplier bill; current report separately applies official weekend/cache quote to usage. No automatic reservation refunds.'},
              'reported_models': dict(Counter(r.get('reported_model') or 'unknown' for r in calls)),
              'independence': 'single_model_repeated_pass', 'accuracy_claim': False, 'independent_human': False,
              'input_sha256': file_sha(input_path), 'manifest_sha256': file_sha(manifest_path), 'config_sha256': file_sha(run / 'approved-config.json'),
              'runtime_source_sha256': binding['source_sha256'], 'holdout_tuning': False, 'downstream_paid_agent_eval': False,
              'checks': {'full_input_identity': True, 'unique_receipt_phases': True, 'first_prompts_blind': True, 'prompt_input_binding': True,
                         'valid_spans_rechecked': True, 'cost_ledger_equal': True, 'within_hard_caps': True, 'all_300_exported': True}}
    output.mkdir(parents=True)
    write_json(output / 'report.json', result)
    write_json(output / 'labels.json', {'labels': labels})
    write_json(output / 'disagreements-and-review.json', disagreements)
    write_json(output / 'failures.json', validation_failures)
    write_json(output / 'unresolved.json', [r for r in labels if r['status'] == 'unresolved'])
    write_json(output / 'call-costs.json', cost_rows)
    for split in split_summary:
        write_json(output / (split + '-labels.json'), [r for r in labels if r['split'] == split])
    with (output / 'labels.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['review_id', 'split', 'label_source', 'status', 'polarity', 'topics', 'evidence', 'reason', 'first_agreement'])
        writer.writeheader()
        for row in labels:
            label = row['label'] or {}
            writer.writerow({**{k: row[k] for k in ['review_id', 'split', 'label_source', 'status', 'first_agreement']},
                             'polarity': label.get('polarity'), 'topics': json.dumps(label.get('topics'), ensure_ascii=False),
                             'evidence': json.dumps(label.get('evidence'), ensure_ascii=False), 'reason': label.get('reason')})
    references = [run / 'authorization.json', run / 'approved-config.json', run / 'input-binding.json', run / 'source_hashes.json',
                  run / 'model-availability.json', run / 'deepseek-pricing.html', runtime / 'budget-ledger.json', input_path, manifest_path]
    references += list(runtime.glob('call-*.json')) + list(output.glob('*'))
    write_json(output / 'integrity.json', {'status': 'passed', 'references': [{'path': str(p.resolve()), 'sha256': file_sha(p)} for p in references if p.is_file()],
                                         'checks': result['checks'], 'supplier_invoice_verified': False})
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['run', 'input', 'manifest', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(audit(args.run, args.input, args.manifest, args.output), ensure_ascii=False))


if __name__ == '__main__':
    main()
