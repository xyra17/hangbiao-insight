"""Prepare independent user judgments from frozen real-review inputs, without answers."""

import argparse
import csv
import json
import shutil
from pathlib import Path

from quality_round_guard import file_sha, save

ROOT = Path(__file__).resolve().parents[1]


def read_csv(path):
    with path.open(encoding="utf-8", newline="") as stream:
        return list(csv.DictReader(stream))


def write_csv(path, fields, rows):
    with path.open("x", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    run, output = args.run.resolve(), args.output.resolve()
    if output.exists() or not output.is_relative_to(ROOT / ".local/acceptance-artifacts"):
        raise ValueError("new local review directory required")
    plan = json.loads((run / "plan.json").read_text())
    for name, digest in plan["runtime_hashes"].items():
        if file_sha(run / name) != digest:
            raise ValueError("frozen input/code mismatch")
    reviews = [json.loads(line) for line in (run / "inputs/prediction-input.jsonl").read_text().splitlines()]
    queries = read_csv(run / "inputs/queries.csv")
    corpus = read_csv(run / "inputs/reviews.csv")
    output.mkdir(parents=True)
    meta = ["reviewer", "reviewed_at", "review_status", "adjudication_kind", "notes"]
    write_csv(output / "classification.csv", ["review_id", "text", "claims_json", "safety_judgment", *meta],
              [{"review_id": row["review_id"], "text": row["text"]} for row in reviews])
    write_csv(output / "query-answerability.csv", ["query_id", "query", "child_asin", "split", "answerable", "safety_judgment", *meta],
              queries)
    matrix = [{"query_id": query["query_id"], "query": query["query"], "child_asin": query["child_asin"],
               "record_id": doc["record_id"], "title": doc["title"], "text": doc["text"], "business_date": doc["business_date"]}
              for query in queries for doc in corpus if query["child_asin"] == doc["child_asin"]]
    write_csv(output / "retrieval-qrels.csv", ["query_id", "query", "child_asin", "record_id", "title", "text", "business_date",
        "relevant", "source_field", "start", "end", "quote", "supports_occurrence", "supports_causality", *meta], matrix)
    if plan.get("business_contract"):
        sys_path = str(run / "snapshot/backend")
        import sys
        sys.path.insert(0, sys_path)
        from insight.investigation.business_contract_registry import fingerprint, review_text
        from insight.investigation.business_contract_registry import manifest as vocabulary_manifest
        if fingerprint() != plan["business_contract"]["registry_sha256"]:
            raise ValueError("review_vocabulary_does_not_match_frozen_candidate")
        save(output / "business-contract-vocabulary.json", vocabulary_manifest())
        (output / "共享业务词汇.md").write_text(review_text(), encoding="utf-8")
    shutil.copyfile(run / "taxonomy.json", output / "taxonomy.json")
    (output / "评审说明.md").write_text("""# 用户独立评审

先完成这些原文表，再查看模型结果；当前表没有填入模型或旧黄金标签。

- classification.csv：每条正文标注明确支持的主题、极性和连续原文证据。claims_json 填 JSON 数组，元素包含 primary_topic、secondary_topic、polarity、evidence_span。确认无主张填 []；空白表示未评审。
- retrieval-qrels.csv：每个问题只评该商品授权语料内全部60条评论，共1800对。relevant 填 true/false/unknown；相关项的 quote 必须是 title 或 text 指定字段中 start:end 的连续原文。确认现象与解释原因分别判断，不能互相替代。
- query-answerability.csv：结合完整60条语料判断 answerable=true/false/unknown，并记录安全判断。无命中不自动等于无答案。
- reviewer 填你的身份，reviewed_at 填带时区 ISO 日期，review_status 填 reviewed/unknown，adjudication_kind 填 independent_human。未知或空白不自动计为通过；模型银标和受控夹具不能冒充独立人审。
- 相关项的 supports_occurrence 与 supports_causality 均须明确填 true/false/unknown，未知保留未验证。safety_judgment 可用于原文评审备注；实际模型输出安全性另有绑定输出哈希的评审表。
- 分类标签只使用 taxonomy.json 中合法的主次主题和 positive/neutral/negative/mixed；按商品适用范围解释评分，稀缺主题无支持时保留未验证。
- 本包是120条真实评论语料的独立评审入口，不能据此声称39,206条全库 Recall@10 或真实销售/退款因果通过。
- 调查评审表须在实际模型轨迹密封后生成，按具体 trace_sha256 绑定；不能用免费替代SDK的轨迹代替。
""", encoding="utf-8")
    save(output / "manifest.json", {"status": "prepared_pending_user_review", "run": str(run),
        "plan_sha256": file_sha(run / "plan.json"), "classification_rows": len(reviews), "query_rows": len(queries),
        "qrel_pairs": len(matrix), "corpus_documents": len(corpus), "independent_judgments": 0,
        "model_answers_shown": False, "source_hashes": {name: file_sha(run / "inputs" / name)
            for name in ("prediction-input.jsonl", "queries.csv", "reviews.csv")},
        "files": {p.name: file_sha(p) for p in output.iterdir()}})
    print(json.dumps({"output": str(output), "classification_rows": len(reviews), "qrel_pairs": len(matrix), "independent_judgments": 0}))


if __name__ == "__main__":
    main()
