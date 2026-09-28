"""Run the Stage 5.5A Golden Dev baseline without touching other splits."""

from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config import DEEPSEEK_FALLBACK_CONFIG, LOCAL_DEV_CONFIG, LOCAL_DEV_V12_CONFIG, OPENAI_PRODUCTION_CONFIG, ProviderConfig
from .evaluator import SemanticEvaluator
from .guard import SemanticGuard
from .model_gateway import DeepSeekResponsesGateway, LMStudioChatGateway, ModelGateway, ModelGatewayError, OpenAIResponsesGateway
from .parser import SemanticParseError, SemanticParser
from .prompt_builder import PromptBuilder
from .schemas import PredictionStatus, ReviewInput, SemanticPrediction
from .taxonomy import TaxonomyLoader


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _write_json_exclusive(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class RunRecorder:
    def __init__(self, output_dir: Path, *, resume: bool = False) -> None:
        output_dir.mkdir(parents=True, exist_ok=resume)
        self.output_dir = output_dir
        self.event_path = output_dir / "run_events.jsonl"
        self.prediction_path = output_dir / "predictions.jsonl"

    def event(self, event_type: str, *, review_id: str | None = None, **details: Any) -> None:
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "event_type": event_type,
            "review_id": review_id,
            **details,
        }
        with self.event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def raw(self, review_id: str, value: Any) -> None:
        _write_json_exclusive(self.output_dir / "raw" / f"{review_id}.json", value)

    def parsed(self, review_id: str, value: Any) -> None:
        _write_json_exclusive(self.output_dir / "parsed" / f"{review_id}.json", value)

    def predictions(self, values: list[SemanticPrediction]) -> None:
        with self.prediction_path.open("a", encoding="utf-8") as handle:
            for value in values:
                handle.write(value.model_dump_json() + "\n")


def _review_input(row: dict[str, str]) -> ReviewInput:
    return ReviewInput(
        review_id=row["review_id"],
        product_id=row["product_id"],
        title=row.get("title", ""),
        text=row["text"],
        rating=float(row["rating"]) if row.get("rating") else None,
        created_at=None,
    )


def _error_types(
    golden: list[dict[str, str]],
    predicted: list[SemanticPrediction],
    processing_errors: list[str],
) -> list[str]:
    errors = list(processing_errors)
    gold_pairs = {(c["secondary_topic"], c["polarity"]) for c in golden}
    pred_pairs = {(c.secondary_topic, str(c.polarity)) for c in predicted}
    gold_topics = {c["secondary_topic"] for c in golden}
    pred_topics = {c.secondary_topic for c in predicted}
    if gold_topics - pred_topics:
        errors.append("missing_topic")
    if pred_topics - gold_topics:
        errors.append("extra_topic")
    if gold_topics & pred_topics and gold_pairs != pred_pairs:
        errors.append("polarity_mismatch")
    if any(c.prediction_status != PredictionStatus.ACCEPTED for c in predicted):
        errors.append("guard_failure")
    return list(dict.fromkeys(errors))


def run_dev_baseline(
    *,
    gateway: ModelGateway,
    review_path: Path,
    claim_path: Path,
    split_path: Path,
    taxonomy_path: Path,
    output_dir: Path,
    resume: bool = False,
) -> dict[str, Any]:
    taxonomy = TaxonomyLoader.load(taxonomy_path)
    if gateway.config.taxonomy_version != taxonomy.version:
        raise ValueError("Gateway taxonomy_version does not match the frozen taxonomy")

    split_rows = _read_csv(split_path)
    dev_ids = {row["review_id"] for row in split_rows if row["split"] == "dev"}
    forbidden_ids = {row["review_id"] for row in split_rows if row["split"] != "dev"}
    if len(dev_ids) != 216:
        raise ValueError(f"Dev split must contain exactly 216 reviews, got {len(dev_ids)}")
    review_rows = [row for row in _read_csv(review_path) if row["review_id"] in dev_ids]
    if len(review_rows) != 216 or {row["review_id"] for row in review_rows} != dev_ids:
        raise ValueError("Golden review table does not match the 216-review Dev split")

    recorder = RunRecorder(output_dir, resume=resume)
    recorder.event(
        "run_started",
        split="dev",
        review_count=216,
        actual_provider_config=gateway.config.model_dump(mode="json"),
        production_provider_config=OPENAI_PRODUCTION_CONFIG.model_dump(mode="json"),
    )
    prompt_builder = PromptBuilder(taxonomy, prompt_version=gateway.config.prompt_version)
    parser = SemanticParser()
    guard = SemanticGuard(taxonomy)
    all_predictions: list[SemanticPrediction] = []
    processing_errors: dict[str, list[str]] = defaultdict(list)
    if resume and (output_dir / "parsed").exists():
        for parsed_path in (output_dir / "parsed").glob("*.json"):
            parsed_value = json.loads(parsed_path.read_text(encoding="utf-8"))
            all_predictions.extend(
                SemanticPrediction.model_validate(value)
                for value in parsed_value.get("predictions", [])
            )

    # Inference reads only review rows and the split manifest. Golden claims are loaded afterwards.
    for index, row in enumerate(review_rows, start=1):
        review = _review_input(row)
        if resume and (output_dir / "parsed" / f"{review.review_id}.json").exists():
            continue
        prompt = prompt_builder.build(review)
        recorder.event("review_started", review_id=review.review_id, index=index, total=216)
        try:
            response = gateway.extract(review, prompt)
            recorder.raw(
                review.review_id,
                {
                    "review_id": review.review_id,
                    "provider": response.provider,
                    "api_name": response.api_name,
                    "model": response.model_name,
                    "reasoning_effort": response.reasoning_effort,
                    "prompt_version": response.prompt_version,
                    "taxonomy_version": response.taxonomy_version,
                    "raw_response_ref": response.raw_response_ref,
                    "raw_response": response.raw_response,
                    "payload": response.payload,
                },
            )
        except ModelGatewayError as exc:
            processing_errors[review.review_id].append("api_error")
            recorder.event(
                "api_error",
                review_id=review.review_id,
                attempts=exc.attempts,
                error=str(exc),
            )
            continue

        try:
            parsed = parser.parse(review, response)
        except SemanticParseError as exc:
            processing_errors[review.review_id].append("schema_validation_error")
            recorder.event("schema_validation_error", review_id=review.review_id, error=str(exc))
            continue

        guarded = guard.apply(review, parsed)
        recorder.parsed(review.review_id, guarded.model_dump(mode="json"))
        recorder.predictions(guarded.predictions)
        all_predictions.extend(guarded.predictions)
        for prediction in guarded.predictions:
            if prediction.prediction_status != PredictionStatus.ACCEPTED:
                recorder.event(
                    "evidence_or_taxonomy_guard_failure",
                    review_id=review.review_id,
                    prediction_id=prediction.prediction_id,
                    status=prediction.prediction_status,
                    quality_flags=prediction.quality_flags,
                )
        recorder.event(
            "review_completed",
            review_id=review.review_id,
            accepted=guarded.accepted_count,
            review_required=guarded.review_required_count,
            rejected=guarded.rejected_count,
        )

    # Evaluation is a separate phase; labels never enter PromptBuilder or ModelGateway.
    golden_claims = [row for row in _read_csv(claim_path) if row["review_id"] in dev_ids]
    if any(row["review_id"] in forbidden_ids for row in golden_claims):
        raise AssertionError("Non-Dev Golden label leakage detected")
    review_source = {_review_input(row).review_id: _review_input(row).source_text for row in review_rows}
    report = SemanticEvaluator(taxonomy).evaluate(
        reviews=review_source,
        golden_claims=golden_claims,
        predictions=all_predictions,
    )

    golden_by_review: dict[str, list[dict[str, str]]] = defaultdict(list)
    predicted_by_review: dict[str, list[SemanticPrediction]] = defaultdict(list)
    for claim in golden_claims:
        golden_by_review[claim["review_id"]].append(claim)
    for claim in all_predictions:
        predicted_by_review[claim.review_id].append(claim)

    bad_case_count = 0
    bad_case_path = output_dir / "bad_cases.jsonl"
    with bad_case_path.open("w", encoding="utf-8") as handle:
        for row in review_rows:
            review_id = row["review_id"]
            errors = _error_types(
                golden_by_review[review_id],
                predicted_by_review[review_id],
                processing_errors.get(review_id, []),
            )
            if not errors:
                continue
            bad_case_count += 1
            record = {
                "review_id": review_id,
                "text": _review_input(row).source_text,
                "golden_claims": golden_by_review[review_id],
                "predicted_claims": [
                    claim.model_dump(mode="json") for claim in predicted_by_review[review_id]
                ],
                "error_type": errors,
                "model": gateway.config.model,
                "prompt_version": gateway.config.prompt_version,
            }
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    result = {
        "run_status": "completed",
        "split": "dev",
        "review_count": len(review_rows),
        "golden_claim_count": len(golden_claims),
        "prediction_count": len(all_predictions),
        "processing_error_review_count": len(processing_errors),
        "bad_case_count": bad_case_count,
        "actual_provider_config": gateway.config.model_dump(mode="json"),
        "production_provider_config": OPENAI_PRODUCTION_CONFIG.model_dump(mode="json"),
        "metrics": report.model_dump(mode="json"),
    }
    _write_json(output_dir / "dev_eval_metrics.json", result)
    recorder.event(
        "run_completed",
        prediction_count=len(all_predictions),
        processing_error_review_count=len(processing_errors),
        bad_case_count=bad_case_count,
    )
    return result


def _gateway(provider: str, prompt_version: str = "semantic-prompt-v1.1") -> ModelGateway:
    if provider == "openai":
        return OpenAIResponsesGateway(config=OPENAI_PRODUCTION_CONFIG)
    if provider == "deepseek":
        return DeepSeekResponsesGateway(config=DEEPSEEK_FALLBACK_CONFIG)
    config = LOCAL_DEV_V12_CONFIG if prompt_version == "semantic-prompt-v1.2" else LOCAL_DEV_CONFIG
    return LMStudioChatGateway(config=config)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=("openai", "lmstudio", "deepseek"), required=True)
    parser.add_argument("--reviews", type=Path, required=True)
    parser.add_argument("--claims", type=Path, required=True)
    parser.add_argument("--split-file", type=Path, required=True)
    parser.add_argument("--taxonomy", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--prompt-version", choices=("semantic-prompt-v1.0", "semantic-prompt-v1.1", "semantic-prompt-v1.2"), default="semantic-prompt-v1.1")
    parser.add_argument("--resume", action="store_true")
    args = parser.parse_args()
    result = run_dev_baseline(
        gateway=_gateway(args.provider, args.prompt_version),
        review_path=args.reviews,
        claim_path=args.claims,
        split_path=args.split_file,
        taxonomy_path=args.taxonomy,
        output_dir=args.output_dir,
        resume=args.resume,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
