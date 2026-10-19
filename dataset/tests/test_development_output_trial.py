"""End-to-end synthetic SDK trial; no network and no source labels."""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize("missing_reasoning_usage", [False, True])
def test_trial_worker_and_postseal_summary_without_network(
    tmp_path, missing_reasoning_usage
):
    run = tmp_path / "synthetic-trial"
    run.mkdir()
    (run / "snapshot/backend/app/semantic").mkdir(parents=True)
    (run / "snapshot/backend/app/__init__.py").write_text("")
    for p in (ROOT / "backend/app/semantic").glob("*.py"):
        shutil.copyfile(p, run / "snapshot/backend/app/semantic" / p.name)
    shutil.copyfile(
        ROOT / "dataset/data/03_黄金评论集/主题分类体系_v1_1.json",
        run / "taxonomy.json",
    )
    for name in [
        "run_development_output_trial.py",
        "summarize_development_output_trial.py",
    ]:
        shutil.copyfile(ROOT / "scripts" / name, run / name)
    rows = [{"review_id": f"synthetic-{i}", "text": "works"} for i in range(20)]
    (run / "prediction-input.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows)
    )
    sys.path.insert(0, str(ROOT / "backend"))
    from app.semantic.prompt_builder import PromptBuilder
    from app.semantic.schemas import ReviewInput
    from app.semantic.taxonomy import TaxonomyLoader

    prompt = PromptBuilder(
        TaxonomyLoader.load(run / "taxonomy.json"),
        prompt_version="semantic-prompt-v1.2",
    ).build(
        ReviewInput(
            review_id="runtime-review", product_id="runtime-product", text="works"
        )
    )
    original_request = {
        "model": "deepseek-flash",
        "input": [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user},
        ],
        "reasoning": {"effort": "low"},
        "text": {
            "format": {
                "type": "json_schema",
                "name": "review_semantic_extraction",
                "schema": prompt.response_schema,
                "strict": True,
            }
        },
        "store": False,
        "max_output_tokens": 1200,
    }
    baseline = [
        {
            "review_id": row["review_id"],
            "split": "development",
            "stratum": "truncated" if index < 10 else "completed",
            "baseline_request_sha256": hashlib.sha256(
                json.dumps(
                    original_request, sort_keys=True, ensure_ascii=False
                ).encode()
            ).hexdigest(),
        }
        for index, row in enumerate(rows)
    ]
    (run / "sampling-baseline.json").write_text(json.dumps({"sample": baseline}))
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()
    plan = {
        "repository": str(ROOT),
        "runtime_references": [
            {"path": str(p.relative_to(run)), "sha256": sha(p)}
            for p in run.rglob("*")
            if p.is_file()
        ],
    }
    (run / "plan.json").write_text(json.dumps(plan))
    (run / "approval.json").write_text(
        json.dumps(
            {
                "approved": True,
                "plan_sha256": sha(run / "plan.json"),
                "max_total_usd": "0.08",
                "max_call_usd": "0.004",
                "transfer_confirmed": True,
            }
        )
    )
    # Inject SDK module before the worker starts. This client has no network code.
    child = """import json, sys, types, importlib.util
from pathlib import Path
run=Path(sys.argv[1]);requests=[]
class Response:
    status='completed';incomplete_details=None;model='deepseek-flash';output_text='{"claims":[]}'
    def __init__(self):self.id='synthetic-'+str(len(requests))
    def model_dump(self,**kwargs):return {'id':self.id,'model':self.model,'status':self.status,'output':[{'type':'message','content':[{'type':'output_text','text':self.output_text}]}],'usage':{'input_tokens':100,'output_tokens':10,'input_tokens_details':{'cached_tokens':0},'output_tokens_details':{'reasoning_tokens':0}}}
class Client:
    def __init__(self,**kwargs):pass
    @property
    def responses(self):return self
    def create(self,**kwargs):requests.append(kwargs);return Response()
sys.modules['openai']=types.SimpleNamespace(OpenAI=Client)
import os
os.environ['DEEPSEEK_API_KEY']='synthetic-not-a-credential'
spec=importlib.util.spec_from_file_location('trial',run/'run_development_output_trial.py');worker=importlib.util.module_from_spec(spec);spec.loader.exec_module(worker)
sys.argv=['trial','--run',str(run),'--paid'];worker.main()
(run/'synthetic-requests.json').write_text(json.dumps(requests))
"""
    if missing_reasoning_usage:
        child = child.replace("'reasoning_tokens':0", "'reasoning_tokens':None")
    result = subprocess.run(
        [sys.executable, "-c", child, str(run)],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    requests = json.loads((run / "synthetic-requests.json").read_text())
    assert len(requests) == 20
    for index, request in enumerate(requests):
        old = {**request, "reasoning": {"effort": "low"}}
        assert (
            hashlib.sha256(
                json.dumps(old, sort_keys=True, ensure_ascii=False).encode()
            ).hexdigest()
            == baseline[index]["baseline_request_sha256"]
        )
    output = run / "summary.json"
    result = subprocess.run(
        [
            sys.executable,
            str(run / "summarize_development_output_trial.py"),
            "--run",
            str(run),
            "--output",
            str(output),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(output.read_text())
    assert (
        report["status"]
        == (
            "trial_criteria_not_met"
            if missing_reasoning_usage
            else "trial_criteria_met"
        )
        and report["model_calls"] == 20
        and report["reported_reasoning_tokens"] == 0
    )
    assert (
        report["strata"]["truncated"]["strict_parser_success"] == 10
        and report["full_300_proof"] is False
    )
    assert report["reasoning_usage_unknown_rows"] == (
        20 if missing_reasoning_usage else 0
    )
    # Hash tampering fails before any results are accepted.
    (run / "raw-results.json").write_text("{}")
    result = subprocess.run(
        [
            sys.executable,
            str(run / "summarize_development_output_trial.py"),
            "--run",
            str(run),
            "--output",
            str(run / "tampered.json"),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0 and "prediction seal mismatch" in result.stderr
