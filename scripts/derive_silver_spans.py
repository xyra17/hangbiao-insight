"""Develop, freeze, then mechanically derive existing silver receipts. Never call a model."""
import argparse
import copy
import csv
import json
import re
import shutil
from collections import Counter
from pathlib import Path

from ai_silver_label import file_sha, load_rows, same_label, validate_label
from quote_span_protocol import VERSION, SpanProtocolError, relocate

ROOT = Path(__file__).resolve().parents[1]
CODE_FILES = ['scripts/quote_span_protocol.py', 'scripts/derive_silver_spans.py', 'scripts/ai_silver_label.py',
              'dataset/tests/test_quote_span_protocol.py']
SELECTION = 'preserve all previously usable row labels; for previously unresolved rows only repair the recorded final review proposal; no first-pass fallback or uncertainty promotion'


def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')


def derive_call(path, text):
    call = json.loads(path.read_text())
    result = {'source_call_id': call['call_id'], 'source_call_path': str(path.resolve()), 'source_call_sha256': file_sha(path),
              'phase': call['phase'], 'review_id': call['review_id'], 'original_status': call['status'],
              'original_validation_error': call.get('validation_error'), 'rule_version': VERSION,
              'candidate': None, 'offset_derivations': [], 'original_proposal': None}
    if call.get('label') is not None:
        validate_label(call['label'], text)
        result.update(candidate=copy.deepcopy(call['label']), processing='original_valid_preserved_no_relocalization')
        return result
    try:
        raw = json.loads(call['raw_response']['choices'][0]['message']['content'])
        result['original_proposal'] = raw
    except (KeyError, IndexError, TypeError, ValueError):
        result.update(processing='unresolved', repair_error=call.get('error_type') or 'missing_or_unparseable_response')
        return result
    if call.get('validation_error') != 'unsupported evidence span':
        result.update(processing='unresolved', repair_error=call.get('validation_error') or 'outside_offset_repair_scope')
        return result
    try:
        value, receipts = relocate(raw, text)
        result.update(candidate=value, offset_derivations=receipts, processing='derived_exact_quote_offsets')
    except SpanProtocolError as exc:
        result.update(processing='unresolved', repair_error=str(exc))
    return result


def derive(source, rows_path, manifest, output, development_only):
    rows = load_rows(rows_path, manifest)
    if development_only:
        rows = [row for row in rows if row['split'] == 'development']
        originals = json.loads((source / 'delivery/development-labels.json').read_text())
    else:
        originals = json.loads((source / 'delivery/labels.json').read_text())['labels']
    by_id = {row['review_id']: row for row in rows}
    assert {row['review_id'] for row in originals} == set(by_id)
    results, calls, disagreements, semantic = [], {}, [], []
    for old in originals:
        row = by_id[old['review_id']]
        ids = old['first_call_ids'] + ([old['review_call_id']] if old['review_call_id'] else [])
        derived = []
        for call_id in ids:
            path = source / 'runtime' / f'call-{call_id:04d}.json'
            value = derive_call(path, row['text'])
            assert value['review_id'] == row['review_id']
            calls[call_id] = value
            derived.append(value)
        first = [d['candidate'] for d in derived[:2]]
        first_agreement = same_label(*first)
        if old['status'] != 'unresolved':
            selected = copy.deepcopy(old['label'])
            status = old['status']
            selected_id = old['first_call_ids'][0] if old['status'] == 'silver_agreement' else old['review_call_id']
            reason = 'original_usable_label_preserved'
        else:
            review = calls.get(old['review_call_id'])
            selected = review['candidate'] if review else None
            if selected and selected['status'] == 'labeled':
                status, reason = 'silver_reviewed_derived_span', 'recorded_final_review_unique_exact_quote_offsets'
            else:
                status, reason = 'unresolved', review.get('repair_error') or ('model_' + selected['status'] if selected else 'missing_final_review')
            selected_id = old['review_call_id']
        if selected:
            validate_label(selected, row['text'])
        if old['status'] != 'unresolved':
            assert selected == old['label']  # Original 63 labels never change polarity/topics/offsets.
        result = {**row, 'label_source': 'ai_generated/silver_derived_from_existing_receipts', 'status': status, 'label': selected,
                  'original_status': old['status'], 'original_label': old['label'], 'processing_reason': reason,
                  'selected_source_call_id': selected_id, 'source_call_ids': ids,
                  'source_call_hashes': {str(i): calls[i]['source_call_sha256'] for i in ids},
                  'derived_first_agreement': first_agreement, 'rule_version': VERSION, 'accuracy_claim': False}
        results.append(result)
        if not first_agreement:
            disagreements.append({'review_id': row['review_id'], 'split': row['split'], 'derived_first_proposals': first,
                                  'final_recorded_review': derived[2]['candidate'] if len(derived) == 3 else None,
                                  'final_status': status, 'source_call_ids': ids})
        if all(p and p['status'] == 'labeled' for p in first) and not first_agreement:
            semantic.append({'review_id': row['review_id'], 'split': row['split'], 'kind': 'first_proposals_disagree_semantically',
                             'source_call_ids': ids, 'resolution': 'unchanged; outside mechanical position repair scope'})
        if first_agreement and selected and selected['status'] == 'labeled' and not same_label(first[0], selected):
            semantic.append({'review_id': row['review_id'], 'split': row['split'], 'kind': 'final_review_differs_from_first_consensus',
                             'source_call_ids': ids, 'resolution': 'recorded final review retained; no polarity/topic edit'})
    summary = {'rule_version': VERSION, 'selection_policy': SELECTION, 'rows': len(results), 'source_run': str(source.resolve()),
               'new_paid_requests': 0, 'new_model_calls': 0, 'independence': 'single_model_repeated_pass',
               'accuracy_claim': False, 'new_blind_test': False, 'supplier_training_unseen': False,
               'original_usable_rows_preserved': sum(row['original_status'] != 'unresolved' for row in results),
               'before_usable': sum(row['original_status'] != 'unresolved' for row in results),
               'after_usable': sum(row['status'] != 'unresolved' for row in results),
               'after_unresolved': sum(row['status'] == 'unresolved' for row in results),
               'call_processing': dict(Counter(row['processing'] for row in calls.values())),
               'still_failed_call_reasons': dict(Counter(row.get('repair_error') for row in calls.values() if row['processing'] == 'unresolved')),
               'semantic_issues_not_resolved': len(semantic),
               'split_comparison': {split: {'rows': sum(r['split'] == split for r in results),
                    'before_usable': sum(r['split'] == split and r['original_status'] != 'unresolved' for r in results),
                    'after_usable': sum(r['split'] == split and r['status'] != 'unresolved' for r in results),
                    'after_unresolved': sum(r['split'] == split and r['status'] == 'unresolved' for r in results)}
                    for split in sorted({r['split'] for r in results})}}
    write(output / 'summary.json', summary)
    write(output / 'labels.json', {'labels': results})
    write(output / 'call-derivations.json', list(calls.values()))
    write(output / 'disagreements.json', disagreements)
    write(output / 'semantic-issues.json', semantic)
    write(output / 'still-failed-calls.json', [c for c in calls.values() if c['processing'] == 'unresolved'])
    write(output / 'unresolved.json', [r for r in results if r['status'] == 'unresolved'])
    for split in summary['split_comparison']:
        write(output / (split + '-labels.json'), [r for r in results if r['split'] == split])
    with (output / 'labels.csv').open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['review_id', 'split', 'label_source', 'original_status', 'status', 'selected_source_call_id', 'polarity', 'topics', 'evidence', 'reason'])
        writer.writeheader()
        for row in results:
            v = row['label'] or {}
            writer.writerow({**{k: row[k] for k in ['review_id', 'split', 'label_source', 'original_status', 'status', 'selected_source_call_id']},
                             'polarity': v.get('polarity'), 'topics': json.dumps(v.get('topics')), 'evidence': json.dumps(v.get('evidence'), ensure_ascii=False), 'reason': v.get('reason')})
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stage', choices=['development', 'freeze', 'apply'], required=True)
    for name in ['source-run', 'input', 'manifest', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    for name in ['development-dir', 'tests-log', 'freeze-dir']:
        parser.add_argument('--' + name, type=Path)
    args = parser.parse_args()
    source = args.source_run.resolve()
    output = args.output.resolve()
    if output.exists() or output.is_relative_to(source) or not output.is_relative_to(ROOT / '.local/acceptance-artifacts'):
        raise ValueError('unique separate local output required')
    code = {name: file_sha(ROOT / name) for name in CODE_FILES}
    input_binding = {'source_integrity_sha256': file_sha(source / 'final-integrity.json'),
                     'input_sha256': file_sha(args.input), 'manifest_sha256': file_sha(args.manifest),
                     'original_report_sha256': file_sha(source / 'delivery/report.json')}
    if args.stage == 'freeze':
        dev = json.loads((args.development_dir / 'summary.json').read_text())
        assert set(dev['split_comparison']) == {'development'}
        log = args.tests_log.read_text()
        assert re.search(r'\d+ passed', log) and not re.search(r'\d+ failed|FAILED|ERROR', log)
        assert code == json.loads((args.development_dir / 'code-hashes.json').read_text())
        output.mkdir(parents=True)
        for name in CODE_FILES:
            target = output / 'code' / name
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(ROOT / name, target)
        write(output / 'freeze.json', {'rule_version': VERSION, 'selection_policy': SELECTION, 'code_hashes': code,
                                      'input_binding': input_binding, 'development_summary_sha256': file_sha(args.development_dir / 'summary.json'),
                                      'boundary_tests_log_sha256': file_sha(args.tests_log), 'frozen_before_validation_holdout_application': True})
        print('protocol frozen; no model calls')
        return
    if args.stage == 'apply':
        frozen = json.loads((args.freeze_dir / 'freeze.json').read_text())
        assert frozen['code_hashes'] == code and frozen['input_binding'] == input_binding
        assert frozen['selection_policy'] == SELECTION and frozen['rule_version'] == VERSION
    output.mkdir(parents=True)
    write(output / 'code-hashes.json', code)
    write(output / 'input-binding.json', input_binding)
    summary = derive(source, args.input, args.manifest, output, args.stage == 'development')
    if args.stage == 'apply':
        write(output / 'protocol-binding.json', {'freeze_path': str(args.freeze_dir.resolve()), 'freeze_sha256': file_sha(args.freeze_dir / 'freeze.json')})
        write(output / 'original-billing-unchanged.json', {'source_report_sha256': input_binding['original_report_sha256'],
                                                        'billing': json.loads((source / 'delivery/report.json').read_text())['billing'], 'new_paid_requests': 0})
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == '__main__':
    main()
