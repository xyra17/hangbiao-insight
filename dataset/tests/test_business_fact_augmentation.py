import csv
import hashlib
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "dataset" / "04_generate_business_facts.py"
CONVERTER_PATH = ROOT / "dataset" / "prepare_business_facts_import.py"
VALIDATOR_PATH = ROOT / "dataset" / "validate_business_facts.py"


def load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


generator = load_module("business_facts_generator", GENERATOR_PATH)
converter = load_module("business_facts_converter", CONVERTER_PATH)
validator = load_module("business_facts_validator", VALIDATOR_PATH)


class BusinessFactAugmentationTests(unittest.TestCase):
    def test_generation_validation_and_import_conversion(self):
        with tempfile.TemporaryDirectory() as temp:
            data_dir = Path(temp) / "04_经营事实增强"
            manifest = generator.generate(data_dir, seed=20260929, generated_at="2026-10-01T00:00:00Z")
            report = validator.validate(data_dir)
            self.assertEqual(report["status"], "passed")
            self.assertGreaterEqual(len(report["checks"]), 60)
            self.assertEqual(manifest["files"]["support_cases_v1.csv"]["rows"], 658)
            self.assertEqual(manifest["files"]["sales_traffic_daily_v1.csv"]["rows"], 5952)
            self.assertIn("unknown_currency", report["boundary_counts"])

            import_dir = data_dir / "import_ready"
            converter.convert(data_dir, import_dir)
            sidecar = json.loads((import_dir / "support_cases_v1.csv.meta.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["source_payload"]["kind"], "support")
            with (import_dir / "support_cases_v1.csv").open(encoding="utf-8", newline="") as handle:
                reader = csv.DictReader(handle)
                expected_fields = manifest["files"]["support_cases_v1.csv"]["contract_fields"] + generator.METADATA_FIELDS
                self.assertEqual(reader.fieldnames, expected_fields)
                first = next(reader)
            self.assertTrue(first["text"].startswith("[SYNTHETIC SUPPORT CASE]"))
            self.assertTrue(first["synthetic_from_review"])
            self.assertNotIn("golden_theme", first["text"])
            self.assertNotIn("primary_topic", reader.fieldnames)

    def test_fixed_seed_and_timestamp_are_byte_reproducible(self):
        with tempfile.TemporaryDirectory() as temp:
            first_dir = Path(temp) / "first"
            second_dir = Path(temp) / "second"
            fixed_time = "2026-10-01T00:00:00Z"
            generator.generate(first_dir, seed=9876, generated_at=fixed_time)
            generator.generate(second_dir, seed=9876, generated_at=fixed_time)
            files = [*generator.CONTRACTS, "manifest.json", "expected_results.json"]
            for relative in files:
                first = first_dir / relative
                second = second_dir / relative
                first_hash = hashlib.sha256(first.read_bytes()).hexdigest()
                second_hash = hashlib.sha256(second.read_bytes()).hexdigest()
                self.assertEqual(first_hash, second_hash, relative)


if __name__ == "__main__":
    unittest.main()
