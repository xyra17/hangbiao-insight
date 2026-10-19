# S5 动作效果验收场景 v1

本包将近期经营闭环中的 6 类动作结局整理为 API 流程模板：改善、无显著改善、恶化、窗口未成熟、缺失数据、关闭后新证据复发。它们是可执行的合成预期，不是因果结论，也不是人工评定的业务标签。

`scenario_catalog.csv` 给出场景 ASIN、指标、生效日、预期状态和停止原因；销量的 `baseline`/`observation` 已换算为每日均值，并保留原窗口总量、天数和起止日期。`task_templates.json` 给出创建内部动作任务的占位 payload。将 `<隔离验收店铺 UUID>`、负责人 ID、事件 UUID 和快照 UUID 替换为隔离环境 API 返回值。所有正式动作需先记录实际执行、生效时间和观察指标；仅在完整观察窗口和成熟队列可比较时完成复查。

关闭后复发场景依赖 `../05_近期经营闭环_v1/` 的 `follow_up_recurrence/`。先扫描创建销售事件，使用动作与复查 API 记录观察，关闭事件，再单独导入 7 天复发夹具并再次扫描。不可提前导入夹具。

```bash
python dataset/05_generate_extended_acceptance_packs.py --pack actions --as-of 2026-10-01 --seed 20261001 --generated-at 2026-10-01T00:00:00Z
python dataset/validate_acceptance_packs.py --as-of 2026-10-01
```

本地验收仅说明合成情景的工作流行为。指标改善不证明动作造成改善。
