# 近期经营闭环验收包 v1

本目录提供近期经营闭环的可导入合成事实与确定性预期结果，供隔离验收店铺验证导入、覆盖判断、指标扫描、事件、调查、动作复查和新证据复发流程。

## 范围

- 站点时区：`America/Los_Angeles`；`as_of` 为 2026-10-01，常规覆盖期为 2026-06-03 至 2026-09-30，共 120 个站点日。
- 10 个 ASIN、20 个 SKU，含 FBA 和 FBM。日销售汇总由订单行生成，可按 ASIN 与日期核对。
- `import_ready/` 的主包包含销售与 Sessions、订单、退款、退货、库存、评分、运营、合成评论和合成客服资料。退款冲正沿用同一币种和交易链。
- `boundary_cases/` 的缺报样本独立覆盖 119/120 天，且没有声明完整。不要和完整主包合并发布。
- `follow_up_recurrence/` 的 7 天销售与订单夹具对应 `closed_then_recurrence` ASIN。只在相关事件关闭后导入，再扫描以检查新证据是否重开事件。
- 主包、边界夹具和后续复发夹具均由固定种子生成。评论与客服文本明确标记为合成内容，不来自原始评论或冻结黄金集。

## 导入和验证

每个 CSV 旁的 `.meta.json` 包含来源契约和上传声明。先在隔离店铺创建或复用来源，并在该店铺映射 manifest 中列出的 ASIN/SKU；替换 sidecar 中的 `<隔离验收店铺 UUID>`。上传后由 `parse_import` 作业解析，确认预览无错误后再发布。复发文件应在事件关闭之后单独上传。

```bash
python dataset/05_generate_extended_acceptance_packs.py --pack loop --as-of 2026-10-01 --seed 20261001 --generated-at 2026-10-01T00:00:00Z
python dataset/validate_acceptance_packs.py --as-of 2026-10-01
cd backend && uv run pytest -c pytest-v2.ini tests_v2/test_extended_acceptance_workflow.py
```

`manifest.json` 与 `expected_results.json` 分别记录包范围、覆盖和逐窗口预期。`validation/unified_validation.json` 是本地结构与数据契约检查记录。以上结果只证明合成夹具和确定性代码路径可用，不代表真实商家数据或生产环境验收。

