import csv
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location('quality_readiness', Path(__file__).resolve().parents[2] / 'scripts/prepare_quality_readiness.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def fixture(tmp_path):
    index = tmp_path / 'index.csv'
    with index.open('w', newline='') as stream:
        writer = csv.DictWriter(stream, fieldnames=['canonical_product', 'normalized_content_sha256', 'created_at'])
        writer.writeheader()
        writer.writerows([{'canonical_product': product, 'normalized_content_sha256': value, 'created_at': '2023-01-01'} for product, value in [('A', 'same'), ('B', 'same'), ('C', 'other')]])
    inventory = tmp_path / 'inventory.json'
    inventory.write_text(json.dumps({'candidate_index': {'sha256': hashlib.sha256(index.read_bytes()).hexdigest()}}))
    return index, inventory


def test_duplicate_connected_products_cannot_create_blind_splits(tmp_path):
    index, inventory = fixture(tmp_path)
    result = module.prepare(index, inventory, tmp_path / 'package')
    assert result['component_count'] == 2 and result['minimum_additional_independent_components'] == 1
    assert not result['split_assigned'] and not result['fresh_holdout_established']
    assert result['independent_human_labels_created'] == 0
    assert json.loads((tmp_path / 'package/model-approval.json').read_text())['approved'] is False


def test_wrong_input_hash_and_existing_output_are_rejected(tmp_path):
    index, inventory = fixture(tmp_path)
    module.prepare(index, inventory, tmp_path / 'package')
    with pytest.raises(ValueError):
        module.prepare(index, inventory, tmp_path / 'package')
    index.write_text('changed')
    with pytest.raises(ValueError):
        module.prepare(index, inventory, tmp_path / 'new')
