"""Compare fresh agent predictions to silver labels; never satisfy human acceptance."""
import argparse
import json
from pathlib import Path

from ai_silver_label import TOPICS, digest, file_sha, load_rows, validate_label


def macro_f1(pairs, labels, multilabel=False):
    if not pairs:
        return None
    scores = []
    for label in labels:
        flags = [(label in a, label in b) if multilabel else (a == label, b == label) for a, b in pairs]
        tp = sum(a and b for a, b in flags)
        fp = sum(not a and b for a, b in flags)
        fn = sum(a and not b for a, b in flags)
        scores.append(2 * tp / (2 * tp + fp + fn) if 2 * tp + fp + fn else 0.0)
    return sum(scores) / len(scores)


def score(rows, silver, predictions):
    refs = {row['review_id']: row for row in rows}
    labels = {row['review_id']: row for row in silver['labels']}
    preds = {row['review_id']: row for row in predictions['predictions']}
    if len(labels) != len(silver['labels']) or len(preds) != len(predictions['predictions']) or set(labels) != set(refs) or set(preds) != set(refs):
        raise ValueError('coverage or duplicate identity mismatch')
    if predictions.get('label_source') == 'ai_generated/silver' or not predictions.get('agent_model') or not predictions.get('execution_report_sha256'):
        raise ValueError('declared agent execution provenance required')
    bound = []
    for key, row in refs.items():
        target, pred = labels[key], preds[key]
        expected = digest({'text': row['text']})
        if target['input_sha256'] != expected or pred['input_sha256'] != expected or target['split'] != row['split']:
            raise ValueError('input or split binding mismatch')
        if target['label_source'] != 'ai_generated/silver':
            raise ValueError('silver source required')
        gold = validate_label(target['label'], row['text']) if target['label'] else None
        prediction = validate_label(pred['label'], row['text']) if pred.get('label') else None
        bound.append((row, gold, prediction))
    result = {}
    for split in ['development', 'validation', 'holdout']:
        items = [item for item in bound if item[0]['split'] == split]
        eligible = [(g, p) for _, g, p in items if g and g['status'] == 'labeled']
        comparisons = [(g, p) for g, p in eligible if p and p['status'] == 'labeled']
        polarity = macro_f1([(g['polarity'], p['polarity']) for g, p in comparisons], ['positive', 'negative', 'mixed'])
        topic = macro_f1([(g['topics'], p['topics']) for g, p in comparisons], TOPICS, True)
        result[split] = {'input_rows': len(items), 'silver_resolved_rows': len(eligible), 'compared_rows': len(comparisons),
                         'agent_unknown_or_abstain_rows': len(eligible) - len(comparisons),
                         'coverage': len(comparisons) / len(eligible) if eligible else None,
                         'polarity_macro_f1_vs_silver': polarity, 'topic_macro_f1_vs_silver': topic,
                         'reference_thresholds': {'polarity_macro_f1': .85, 'topic_macro_f1': .85},
                         'threshold_comparison_not_release_gate': polarity is not None and topic is not None and polarity >= .85 and topic >= .85 and len(comparisons) == len(eligible)}
    return {'status': 'silver_comparison_only', 'label_source': 'ai_generated/silver', 'splits': result,
            'original_human_gate_satisfied': False, 'accuracy_claim': False,
            'taxonomy': 'public-silver-v1 7 broad topics; not original charger taxonomy',
            'supplier_training_unseen': False, 'agent_model': predictions['agent_model'],
            'execution_report_sha256': predictions['execution_report_sha256'],
            'execution_provenance': 'declared; CLI verifies report hash, not vendor authentication'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ['input', 'manifest', 'silver', 'predictions', 'execution-report', 'output']:
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError('unique output required')
    rows = load_rows(args.input, args.manifest)
    predictions = json.loads(args.predictions.read_text())
    if predictions.get('execution_report_sha256') != file_sha(args.execution_report):
        raise ValueError('execution report hash mismatch')
    result = score(rows, json.loads(args.silver.read_text()), predictions)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2) + '\n')


if __name__ == '__main__':
    main()
