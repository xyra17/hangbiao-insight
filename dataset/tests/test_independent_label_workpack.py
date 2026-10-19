import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts'))
from prepare_independent_label_workpack import audit, candidate_index, normalized_hash


def test_normalization_deduplicates_case_and_punctuation():
    assert normalized_hash('ＡBC! Test') == normalized_hash('abc test')


def test_known_product_cannot_become_blind_even_with_new_review_id(tmp_path):
    original, seen = tmp_path / 'original.csv', tmp_path / 'seen.csv'
    with original.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['review_id', 'source_asin', 'created_at', 'title', 'text'])
        writer.writeheader()
        writer.writerows([{'review_id': 'new', 'source_asin': 'KNOWN', 'created_at': '2026-10-08', 'text': 'Hello!'},
                          {'review_id': 'unseen', 'source_asin': 'NEW', 'created_at': '2026-10-08', 'text': 'hello'}])
    seen.write_text('review_id,product_id\nold,KNOWN\n')
    result = audit(original, seen)
    assert result['candidate_rows_outside_legacy_gold_products'] == 1
    assert result['normalized_exact_duplicate_excess_rows'] == 1
    assert result['status'] == 'blocked_external'
    assert result['fresh_blind_test_established'] is False


def test_source_variant_does_not_create_unseen_product(tmp_path):
    original, seen = tmp_path / 'original.csv', tmp_path / 'seen.csv'
    original.write_text('review_id,sku,source_asin,created_at,text\nnew,KNOWN,VARIANT,2026-10-08,hello\n')
    seen.write_text('review_id,product_id\nold,KNOWN\n')
    assert audit(original, seen)['candidate_rows_outside_legacy_gold_products'] == 0


def test_candidate_index_exports_no_text_or_labels_and_does_not_assign_split(tmp_path):
    original, seen, output = tmp_path / 'original.csv', tmp_path / 'seen.csv', tmp_path / 'index.csv'
    original.write_text('review_id,sku,source_asin,created_at,text\na,NEW,VARIANT,2026-10-08,private text\n')
    seen.write_text('review_id,product_id\nold,KNOWN\n')
    candidate_index(original, seen, output)
    with output.open() as stream:
        rows = list(csv.DictReader(stream))
    assert len(rows) == 1 and rows[0]['proposed_split'] == ''
    assert 'text' not in rows[0] and 'private text' not in output.read_text()
    assert rows[0]['canonical_product'] == 'NEW'
