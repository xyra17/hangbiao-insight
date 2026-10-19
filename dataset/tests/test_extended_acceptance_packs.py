import csv
import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
GENERATOR_PATH = ROOT / "dataset" / "05_generate_extended_acceptance_packs.py"
SCORER_PATH = ROOT / "dataset" / "score_paired_investigations.py"
SPEC = importlib.util.spec_from_file_location("extended_acceptance_pack_generator", GENERATOR_PATH)
assert SPEC and SPEC.loader
generator = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(generator)
SCORER_SPEC = importlib.util.spec_from_file_location("paired_investigation_scorer", SCORER_PATH)
assert SCORER_SPEC and SCORER_SPEC.loader
scorer = importlib.util.module_from_spec(SCORER_SPEC)
SCORER_SPEC.loader.exec_module(scorer)


def read_rows(path):
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


class ExtendedAcceptancePackTests(unittest.TestCase):
    def test_evaluation_splits_keep_pairs_queries_documents_and_qrels_together(self):
        with tempfile.TemporaryDirectory() as temporary:
            output_root = Path(temporary)
            package = generator.build_eval_pack(
                output_root,
                generator.date.fromisoformat("2026-10-01"),
                seed=42,
                generated_at="2026-10-01T00:00:00Z",
            )
            manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
            pairs = read_rows(package / "paired_investigation_scenarios.csv")
            queries = read_rows(package / "retrieval_queries.csv")
            documents = read_rows(package / "retrieval_documents.csv")
            qrels = read_rows(package / "retrieval_qrels.csv")

            self.assertEqual({row["split"] for row in queries}, set(generator.EVAL_SPLITS))
            self.assertEqual({split: sum(row["split"] == split for row in queries) for split in generator.EVAL_SPLITS}, {
                "development": 10,
                "validation": 10,
                "test": 10,
            })
            pair_groups = {}
            for row in pairs:
                pair_groups.setdefault(row["pair_id"], set()).add(row["split"])
            self.assertTrue(all(len(splits) == 1 for splits in pair_groups.values()))
            query_split = {row["query_id"]: row["split"] for row in queries}
            doc_split = {row["doc_id"]: row["split"] for row in documents}
            self.assertTrue(all(row["split"] == query_split[row["query_id"]] for row in documents))
            self.assertTrue(all(row["split"] == query_split[row["query_id"]] == doc_split[row["doc_id"]] for row in qrels))
            self.assertEqual(manifest["split_policy"]["counts"]["development"]["paired_scenario_pairs"], 8)

    def test_eval_review_ids_do_not_overlap_frozen_golden_corpus(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = generator.build_eval_pack(
                Path(temporary),
                generator.date.fromisoformat("2026-10-01"),
                seed=42,
                generated_at="2026-10-01T00:00:00Z",
            )
            generated_ids = set()
            for filename in (
                "paired_investigation_scenarios.csv",
                "retrieval_documents.csv",
                "rare_topic_extension_candidates.csv",
            ):
                generated_ids.update(row["review_id"] for row in read_rows(package / filename) if row.get("review_id"))
            self.assertFalse(generated_ids & generator.frozen_golden_review_ids())

    def test_pair_contracts_change_decision_and_pair_10_separates_overheating_from_denial(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = generator.build_eval_pack(
                Path(temporary),
                generator.date.fromisoformat("2026-10-01"),
                seed=42,
                generated_at="2026-10-01T00:00:00Z",
            )
            pairs = read_rows(package / "paired_investigation_scenarios.csv")
            groups = {}
            for row in pairs:
                groups.setdefault(row["pair_id"], []).append(row)
            self.assertEqual(len(groups), 24)
            for pair_rows in groups.values():
                self.assertEqual(len(pair_rows), 2)
                for field in ("expected_behavior", "expected_next_step", "allowed_conclusion", "expected_stop_condition"):
                    self.assertEqual(len({row[field] for row in pair_rows}), 2)
            positive = next(row for row in groups["PAIR-10"] if row["evidence_variant"] == "supports_claim")
            negative = next(row for row in groups["PAIR-10"] if row["evidence_variant"] == "changes_decision")
            self.assertEqual(positive["safety_escalation"], "urgent_handoff")
            self.assertEqual(negative["safety_escalation"], "none")
            self.assertNotEqual(positive["allowed_conclusion"], negative["allowed_conclusion"])

    def test_contract_replay_scores_all_pairs_and_rejects_pair_10_false_escalation(self):
        with tempfile.TemporaryDirectory() as temporary:
            package = generator.build_eval_pack(
                Path(temporary),
                generator.date.fromisoformat("2026-10-01"),
                seed=42,
                generated_at="2026-10-01T00:00:00Z",
            )
            gold_rows = read_rows(package / "paired_investigation_scenarios.csv")
            fields = scorer.FIELDS
            predictions = [
                {"scenario_id": row["scenario_id"], **{field: row[field] for field in fields}}
                for row in gold_rows
            ]
            report = scorer.score_rows(gold_rows, predictions, mode="deterministic_contract_replay")
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["pairs_passed"], 24)
            negative = next(row for row in predictions if row["scenario_id"] == "PAIR-10-changes_decision")
            negative["safety_escalation"] = "urgent_handoff"
            failed = scorer.score_rows(gold_rows, predictions, mode="supplied_decision_predictions")
            pair_ten = next(pair for pair in failed["pairs"] if pair["pair_id"] == "PAIR-10")
            self.assertEqual(pair_ten["status"], "failed")
            self.assertFalse(pair_ten["pair_safety_gate_pass"])


if __name__ == "__main__":
    unittest.main()
