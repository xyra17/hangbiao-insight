"""Read-only eligibility audit; never promote seen products or existing labels to blind truth."""
import argparse
import csv
import hashlib
import json
import re
import unicodedata
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def normalized_hash(text):
    normalized = unicodedata.normalize('NFKC', text).casefold()
    normalized = re.sub(r'[^\w]+', ' ', normalized).strip()
    return hashlib.sha256(normalized.encode()).hexdigest()


def audit(original, seen):
    with seen.open(encoding='utf-8-sig', newline='') as stream:
        legacy = list(csv.DictReader(stream))
    products = {row['product_id'] for row in legacy}
    seen_ids = {row['review_id'] for row in legacy}
    counts, hashes, dates = Counter(), Counter(), {}
    hash_products = defaultdict(set)
    eligible, rows, missing = 0, 0, 0
    with original.open(encoding='utf-8-sig', newline='') as stream:
        for row in csv.DictReader(stream):
            product = row.get('sku') or row.get('source_asin', '')
            created = row.get('created_at', '')[:10]
            counts[product] += 1
            rows += 1
            missing += not product or not created
            content_hash = normalized_hash(row.get('title', '') + ' ' + row.get('text', ''))
            hashes[content_hash] += 1
            hash_products[content_hash].add(product)
            if product and created:
                lo, hi = dates.get(product, (created, created))
                dates[product] = (min(lo, created), max(hi, created))
            eligible += bool(product and created and product not in products and row.get('review_id') not in seen_ids)
    return {'status': 'blocked_external', 'dependency': 'E2', 'source_rows': rows,
            'known_evaluation_products': sorted(products), 'product_grouping': 'canonical sku; source_asin variants do not create new products', 'source_products': dict(counts),
            'product_date_ranges': dates, 'missing_product_or_date_rows': missing,
            'normalized_exact_duplicate_excess_rows': sum(n - 1 for n in hashes.values()),
            'cross_product_exact_duplicate_clusters': sum(len(products) > 1 for products in hash_products.values()),
            'candidate_rows_outside_legacy_gold_products': eligible,
            'fresh_blind_test_established': False,
            'reason': 'Eligibility inventory only; prior-exposure audit and approved product/time holdout, near-duplicate review, independent labels and freeze owner required.',
            'inputs': {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in (original, seen)},
            'no_raw_text_exported': True,
            'freeze_requirements': ['disjoint development/validation/test product sets',
                                    'test time after approved cutoff; no future facts used',
                                    'normalized exact and semantic near-duplicate clusters cannot cross splits',
                                    'two independent reviewers plus adjudicator; no model output shown before first labels',
                                    'qrels/no-answer/safety/intent/topic/polarity independently reviewed',
                                    'holdout custodian freezes hashes before runtime; scorer alone reads gold',
                                    'new runtime generates its own eight-field adjudication template',
                                    'legacy 03/07 tests are seen regression only']}


def candidate_index(original, seen, output):
    """IDs, source positions and hashes only; no text, labels or automatic split."""
    with seen.open(encoding='utf-8-sig', newline='') as stream:
        known = list(csv.DictReader(stream))
    products = {row['product_id'] for row in known}
    ids = {row['review_id'] for row in known}
    with original.open(encoding='utf-8-sig', newline='') as source, output.open('w', newline='') as target:
        writer = csv.DictWriter(target, fieldnames=['review_id', 'canonical_product', 'source_asin',
                    'created_at', 'source_line', 'normalized_content_sha256', 'proposed_split', 'eligibility'])
        writer.writeheader()
        for line, row in enumerate(csv.DictReader(source), 2):
            product = row.get('sku') or row.get('source_asin', '')
            if product and row.get('created_at') and product not in products and row.get('review_id') not in ids:
                writer.writerow({'review_id': row['review_id'], 'canonical_product': product,
                    'source_asin': row.get('source_asin', ''), 'created_at': row['created_at'],
                    'source_line': line, 'normalized_content_sha256': normalized_hash(row.get('title', '') + ' ' + row.get('text', '')),
                    'proposed_split': '', 'eligibility': 'pending_exposure_time_near_duplicate_and_independent_review'})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--original', type=Path, default=ROOT / 'dataset/data/01_标准化评论数据/评论数据集_v1.csv')
    parser.add_argument('--seen', type=Path, default=ROOT / 'dataset/data/03_黄金评论集/Golden语义评测切分_v1_0.csv')
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.output.resolve() in {args.original.resolve(), args.seen.resolve()}:
        parser.error('output cannot overwrite input')
    result = audit(args.original, args.seen)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    index = args.output.with_name(args.output.stem + '-candidate-index.csv')
    candidate_index(args.original, args.seen, index)
    result['candidate_index'] = {'path': str(index), 'sha256': hashlib.sha256(index.read_bytes()).hexdigest(), 'raw_text_exported': False, 'split_assigned': False}
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print(json.dumps({key: result[key] for key in ['status', 'source_rows', 'candidate_rows_outside_legacy_gold_products', 'fresh_blind_test_established']}))


if __name__ == '__main__':
    main()
