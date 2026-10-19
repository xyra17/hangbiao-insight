"""Constructed cases only; no validation/holdout labels or paid calls."""

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "backend"))
from app.semantic.config import PRIMARY_SEMANTIC_CONFIG
from app.semantic.factory import build_semantic_service
from app.semantic.model_gateway import (
    DeepSeekResponsesGateway,
    ModelGatewayError,
)
from public_agent_prediction import review_input
from score_public_product_agent import metrics, project, score


def claim(
    primary="function_performance",
    secondary="general_performance",
    polarity="positive",
    status="accepted",
):
    return {
        "primary_topic": primary,
        "secondary_topic": secondary,
        "polarity": polarity,
        "prediction_status": status,
    }


@pytest.mark.parametrize(
    ("claims", "status", "topics", "polarity"),
    [
        ([], "abstain", [], None),
        ([claim()], "labeled", ["function"], "positive"),
        ([claim(polarity="negative"), claim()], "labeled", ["function"], "mixed"),
        ([claim(polarity="neutral")], "neutral_unmapped", ["function"], None),
        ([claim(primary="other_unknown")], "uncertain", [], None),
        (
            [claim(primary="convenience_value", secondary="price_value")],
            "labeled",
            ["value"],
            "positive",
        ),
        ([claim(primary="product_quality")], "labeled", ["other"], "positive"),
        ([claim(primary="safety_risk")], "labeled", ["other"], "positive"),
        (
            [claim(primary="reliability_durability")],
            "labeled",
            ["durability"],
            "positive",
        ),
        (
            [claim(primary="compatibility_fit")],
            "labeled",
            ["compatibility"],
            "positive",
        ),
        ([claim(status="rejected")], "abstain", [], None),
    ],
)
def test_mapping(claims, status, topics, polarity):
    assert project({"predictions": claims}) == {
        "status": status,
        "topics": topics,
        "polarity": polarity,
    }


def test_safe_input():
    row = {"review_id": "local-only", "text": "works"}
    review = review_input(row)
    assert (
        review.review_id == "runtime-review" and review.product_id == "runtime-product"
    )
    for key in ["label", "split", "expected", "topics"]:
        with pytest.raises(ValueError):
            review_input({**row, key: "forbidden"})


def test_read_boundary_in_child(tmp_path):
    script = f"import sys;sys.path.insert(0,{str(ROOT / 'scripts')!r});from public_agent_prediction import install_read_boundary;install_read_boundary({str(ROOT)!r},{str(tmp_path)!r});open({str(ROOT / 'dataset/data/03_黄金评论集/主题分类体系_v1_1.json')!r}).read()"
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, check=False
    )
    assert result.returncode != 0 and "PermissionError" in result.stderr


def test_real_product_chain_and_no_sdk_retry():
    calls = []

    class Client:
        @property
        def responses(self):
            return self

        def create(self, **kwargs):
            calls.append(kwargs)
            return SimpleNamespace(
                output_text=json.dumps(
                    {
                        "claims": [
                            {
                                "primary_topic": "function_performance",
                                "secondary_topic": "general_performance",
                                "polarity": "positive",
                                "evidence_span": "works",
                            }
                        ]
                    }
                ),
                id="fake",
            )

    config = PRIMARY_SEMANTIC_CONFIG.model_copy(
        update={
            "model": "deepseek-flash",
            "max_attempts": 1,
            "prompt_version": "semantic-prompt-v1.2",
        }
    )
    gateway = DeepSeekResponsesGateway(config=config, client=Client())
    service = build_semantic_service(
        ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json", gateway=gateway
    )
    result = service.extract(review_input({"review_id": "id", "text": "works"}))
    assert result.accepted_count == 1 and len(calls) == 1
    assert "Claim-first but evidence-conservative" in calls[0]["input"][0]["content"]
    assert "id" not in json.loads(calls[0]["input"][1]["content"])["review_id"]

    class Broken(Client):
        def create(self, **kwargs):
            calls.append(kwargs)
            raise ConnectionError("fake")

    with pytest.raises(ModelGatewayError):
        DeepSeekResponsesGateway(config=config, client=Broken()).extract(
            review_input({"review_id": "id", "text": "works"}),
            service.prompt_builder.build(
                review_input({"review_id": "id", "text": "works"})
            ),
        )
    assert len(calls) == 2


def test_coverage_and_failure_penalty():
    gold = {"status": "labeled", "polarity": "positive", "topics": ["function"]}
    items = [
        {
            "gold": gold,
            "prediction": {
                "status": "labeled",
                "polarity": "positive",
                "topics": ["function"],
            },
        },
        {
            "gold": gold,
            "prediction": {"status": "failed", "polarity": None, "topics": []},
        },
        {
            "gold": None,
            "prediction": {"status": "abstain", "polarity": None, "topics": []},
        },
    ]
    result = metrics(items)
    assert (
        result["input_rows"] == 3
        and result["label_unresolved"] == 1
        and result["prediction_statuses"]["failed"] == 1
    )
    assert (
        result["conditional_agreement"]["polarity_macro_f1"]
        > result["coverage_penalized_agreement"]["polarity_macro_f1"]
    )


def test_split_binding_and_issue_retention():
    rows = [{"review_id": "a", "text": "works", "split": "development"}]
    labels = [
        {
            **rows[0],
            "label": {
                "status": "labeled",
                "polarity": "positive",
                "topics": ["function"],
            },
        }
    ]
    preds = [
        {"review_id": "a", "status": "completed", "guarded": {"predictions": [claim()]}}
    ]
    result = score(rows, labels, preds, [{"review_id": "a"}])
    assert result["semantic_issue_subset"]["development"]["input_rows"] == 1
    assert result["all_rows"]["input_rows"] == 1 and result["accuracy_claim"] is False
    with pytest.raises(ValueError):
        score(rows, labels, preds * 2, [])
    with pytest.raises(ValueError):
        score(rows, [{**labels[0], "split": "holdout"}], preds, [])


def test_postseal_scoring_and_tamper_rejection(tmp_path, monkeypatch):
    import hashlib

    import score_public_product_agent as module

    def write(name, value):
        path = tmp_path / name
        path.write_text(json.dumps(value))
        return path

    def sha(path):
        return hashlib.sha256(path.read_bytes()).hexdigest()

    rows = [
        {"review_id": s, "text": "works", "split": s}
        for s in ["development", "validation", "holdout"]
    ]
    source = tmp_path / "input.jsonl"
    source.write_text("".join(json.dumps(r) + "\n" for r in rows))
    silver = write(
        "silver.json",
        {
            "labels": [
                {
                    **r,
                    "label": {
                        "status": "labeled",
                        "polarity": "positive",
                        "topics": ["function"],
                    },
                }
                for r in rows
            ]
        },
    )
    issues = write("issues.json", [{"review_id": "holdout"}])
    raw = write(
        "raw-predictions.json",
        {
            "predictions": [
                {
                    "review_id": r["review_id"],
                    "status": "completed",
                    "guarded": {"predictions": [claim()]},
                }
                for r in rows
            ]
        },
    )
    report = write("execution-report.json", {"receipts": []})
    plan = write("plan.json", {"input_sha256": sha(source)})
    write(
        "prediction-seal.json",
        {
            "predictions_sha256": sha(raw),
            "execution_report_sha256": sha(report),
            "plan_sha256": sha(plan),
        },
    )
    output = tmp_path / "scores.json"
    argv = [
        "scorer",
        "--run",
        str(tmp_path),
        "--input",
        str(source),
        "--silver",
        str(silver),
        "--issues",
        str(issues),
        "--output",
        str(output),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    module.main()
    assert (
        json.loads(output.read_text())["semantic_issue_subset"]["holdout"]["input_rows"]
        == 1
    )
    raw.write_text("{}")
    with pytest.raises(ValueError, match="prediction seal mismatch"):
        module.main()
