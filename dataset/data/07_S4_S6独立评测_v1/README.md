# S4 / S6 独立合成评测包 v1

该包包含调查决策成对场景、双语检索查询、合成候选文档、qrels 和稀缺主题候选。调查对以 `pair_id` 分组：同一问题的两种证据变体必须留在同一个开发、验证或测试切分。检索数据以 `query_id` 分组，关联文档和 qrels 必须跟随同一切分。`manifest.json` 提供每个切分的数量。

检索包含 30 个查询（中英各 15 个）、120 个候选文档、22 条相关性判断和 8 个不可回答查询。稀缺主题扩展候选为 `synthetic_unreviewed`，仅用于开发候选池，未经过人工裁决。生成器不读取冻结黄金集；统一校验器会检查合成 `review_id` 与冻结评论 ID 不重叠。

可在生成检索结果后按测试切分报告 Recall@10 等检索指标；这些 qrels 是机器生成夹具，不能解释为独立人工标注的模型质量分数。成对场景用于确定性回归，不替代真实专家评审。

每个成对场景变体都定义预期路由、行为、下一步、允许与禁止结论、安全升级和停止条件。两种证据的关键决策与结论必须随证据变化；PAIR-10 专门检查明确过热与否定过热，前者要求紧急人工交接，后者禁止触发过热事件。

运行确定性契约回放并生成逐对结果：

```bash
python dataset/score_paired_investigations.py --replay-contracts
```

报告写入 `dataset/data/07_S4_S6独立评测_v1/validation/paired_contract_replay_latest.json`。回放将金标准契约与自身比对，用于检查评分入口、成对区分和 PAIR-10 安全门控；它不是 planner 或模型输出，也不是模型质量通过。

真实 planner/模型运行结果可按场景 ID 填入模板并评分：

```bash
python dataset/score_paired_investigations.py --write-template /tmp/paired_predictions.csv --replay-contracts
python dataset/score_paired_investigations.py --predictions /path/to/paired_predictions.csv
```

预测 CSV 字段为 `scenario_id`、`expected_route`、`expected_behavior`、`expected_next_step`、`allowed_conclusion`、`forbidden_conclusion`、`safety_escalation`、`expected_stop_condition` 和 `expected_tool_names`。这只是确定性标签评分；实际模型质量仍需独立人工裁决集和真实模型运行。

### 项目真实运行入口

`backend/tests_v2/test_s4_s6_runtime_acceptance.py` 会把去答案化的30个查询/120个文档导入测试数据库，先调用项目 `/api/v1/evidence-search/`，再通过调查 API、Job runner 和实际工具执行48个场景；所有检索与调查请求结束后才读取 qrels 与场景黄金契约评分。运行不向 planner 传 qrels/expected，也不按 case ID 注入答案。v4 运行输入把“请求选中的 ASIN”和“证据记录所属 ASIN”分开，运行时只将请求范围发给项目 API。

```bash
uv run --directory backend --env-file ../.env.local pytest -c pytest-v2.ini tests_v2/test_s4_s6_runtime_acceptance.py
```

准备好供应商、数据区域、密钥注入、脱敏边界和预算，并明确允许这批合成查询/证据发往该区域后，真实模型入口使用同一运行轨迹：

```bash
INVESTIGATION_EVAL_PROVIDER=deepseek uv run --directory backend --env-file ../.env.local pytest -c pytest-v2.ini tests_v2/test_s4_s6_runtime_acceptance.py
```

这条命令仍走项目授权的结构化 planner、工具白名单、预算账本与恢复路径。缺密钥/供应商授权时不应运行付费调用；缺少适配的结论/禁止声明分类器和人工裁决时，报告会将相应质量字段保留为 unsupported 或 failed。

2026-10-04 v3 结果（历史）：30/30 检索请求成功、Recall@10 overall 0.8182；调查 48/48 运行完成、104 次工具调用，22/24 对出现证据/后续查询差异。对应历史试跑和失败报告保留在 `validation/real_*_2026-10-04.json`。该版本没有达到 0.85 Recall@10 门槛。

2026-10-05 v4 当前本地复跑：通过项目真实检索 API 运行 30/30 查询，120 文档；Recall@10 development 1.0000、validation 1.0000、test 0.6250、overall 0.8636；引用有效率 1.0000，整体 P95 18.144 ms。0.85 的合成总体门槛达到，但 test 切分仍为 0.6250，且 2 个不可回答 test 查询中有 1 个误召回，必须原样报告。PAIR-21 的双语运行证据使后续检索参数随证据变化；PAIR-19 的未选商品请求由真实定位器进入澄清，造成工具轨迹差异。调查 48/48 Job 终态成功，其中 47 completed、1 needs_clarification；共 101 次真实工具调用。24/24 对有工具轨迹或后续查询参数差异，其中 23 对有检索证据及其后的参数差异；PAIR-19 是范围澄清路径，不能计作证据驱动差异。模型契约评分仍为 0/24，144 个行为/结论/安全字段 unsupported；这是确定性 planner 真实项目轨迹，不是真实模型质量通过。PAIR-19 当前仅提交了已选店铺、未选商品，验证的是店铺内商品歧义；冻结黄金中的“未选店铺”描述超出该 API 输入形态，原冻结行未改，完整跨店澄清仍待独立修订/裁决。

当前原始报告：`validation/real_retrieval_runtime_v4_final_2026-10-05.json`、`validation/real_paired_investigation_runtime_v4_final_2026-10-05.json`，以及便于发现的 `*_latest.json`。Corpus v4 的生成入口和输入哈希见 `runtime_corpus_v4/corpus_manifest.json`；冻结的金标准 `paired_investigation_scenarios.csv`、旧 v3 语料和失败报告保留。v4 语料生成命令：

```bash
python dataset/prepare_retrieval_corpus.py --output-dir dataset/data/07_S4_S6独立评测_v1/runtime_corpus_v4 --overwrite
```

别名补充是在查看 dev/validation 漏召回后加入的中英“退款/冲正”通用映射；test qrels 未用于调参。本轮总体 Recall@10 达门槛但 test 仍低，需独立人工标注和真实供应商评测后才能作质量结论。

稀缺主题候选继续标记 `synthetic_unreviewed`。黄金契约回放报告仅验证 scorer，自我对照的24/24不得被列作Agent完成或模型质量分数。真实供应商与独立标注缺失时，S4/S6保持待验收。

```bash
python dataset/05_generate_extended_acceptance_packs.py --pack evaluation --as-of 2026-10-01 --seed 20261001 --generated-at 2026-10-01T00:00:00Z
python dataset/validate_acceptance_packs.py --as-of 2026-10-01
```
