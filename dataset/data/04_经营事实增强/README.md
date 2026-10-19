# 经营事实增强数据包 v1.0

此目录提供三批可重复生成的合成/派生事实，用于项目本地数据契约、计算边界和 UI/API 自动验收。它不包含真实 Amazon 销售、财务、库存、客服或退货报表。标准化真实评论与冻结黄金评论文件未被修改。

## 生成、转换、校验

在仓库根目录运行：

```bash
python dataset/04_generate_business_facts.py --seed 20260929 --generated-at 2026-10-01T00:00:00Z
python dataset/prepare_business_facts_import.py
python dataset/validate_business_facts.py
python -m unittest discover -s dataset/tests -v
```

项目 Django 端字段契约回归：

```bash
cd backend
uv run --env-file ../.env.local pytest -c pytest-v2.ini tests_v2/test_business_fact_package_contract.py -q
```

生成器仅重写本目录的 CSV、`manifest.json`、`expected_results.json` 和夹具目录。固定 seed 与 `--generated-at` 后，主数据、manifest 和预期结果可逐字节复现。转换器在 `import_ready/` 生成 Django 导入字段以及 `.meta.json` sidecar；质量结果写在 `validation/`。

## 批次与覆盖范围

| 批次 | 文件 | 行数 | 覆盖范围 | 主要用途 |
|---|---|---:|---|---|
| 1 | `sales_traffic_daily_v1.csv` | 5,952 | 3 个核心 ASIN；2017-08-19 至 2023-09-02 | sessions、ordered_units、订单数、Buy Box、销售金额 |
| 1 | `ratings_daily_v1.csv` | 5,952 | 同上 | 按商品日截至当日真实评论累计评分均值和数量；不是 Amazon 页面快照 |
| 1 | `inventory_daily_v1.csv` | 5,952 | 同上 | available、reserved、inbound、unsellable、total 与 listing 状态 |
| 1 | `operations_v1.csv` | 414 | 2017-08-19 至 2023-09-01 | 基准价格、月度优惠、与既有合成情景关联的运营事件 |
| 2 | `orders_v1.csv` | 5,958 | 2017-08-19 至 2023-09-02 | 订单行、pending/cancelled/exchange、迟到发货、撤销候选 |
| 2 | `refunds_v1.csv` | 6,082 | 2017-08-19 至 2023-09-02 | 部分/全额、冲正、跨月迟到、多币种及未知币种 |
| 2 | `returns_v1.csv` | 313 | 2017-08-26 至 2023-08-25 | 申请、收到、完成、取消、拒绝、未知来源状态和跨月日期 |
| 3 | `support_cases_v1.csv` | 658 | 2017-12-24 至 2023-06-14 | 黄金主题生成的客服检索夹具，全部明确标为 synthetic |

主 CSV 共 **31,281 行**。输入经营指标本身是合成数据；评论评分是现有公开评论聚合值；客服文本是按冻结评论主题生成的通用模板，没有复制评论正文。客服主题/极性列只留在审计用源 CSV，转换后的可导入 CSV 不带这些标签列；导入器会把 `synthetic_from_review` 与来源标记保存为原始行中的忽略列，事实本身的 `text` 也保留 `[SYNTHETIC SUPPORT CASE]` 标记。

## 当前项目的导入入口

`import_ready/<file>.meta.json` 中的 `source_payload` 对应现有 `POST /api/v1/sources/` 请求体；把 `shop` 占位值替换为隔离验收店铺 ID。每个事实种类只建立一个主来源，同类修订文件后续复用相同来源。`upload_payload` 对应 `POST /api/v1/imports/` 的覆盖日期、生成时间和 ASIN 范围；发布前按现有 API 的首次契约确认流程操作。

订单、退款和退货需要在隔离店铺预先建立三个商品映射：`child_asin` 对应 SKU `<ASIN>-FBA`、`fulfillment=FBA`，映射生效日早于 2017-08-19。商品日数据使用 ASIN 粒度。不要把 `declared_complete=false` 改成完整覆盖；这些历史日期是稀疏采样，缺行代表未知而不是零。

同一业务键的文件必须拆成不同导入批次并按顺序发布：`lifecycle_revisions/sales_traffic_revision_v1.csv` → `sales_traffic_revision_v2.csv`；退货依次上传 requested → received → cancelled 修订。撤销发布示例使用 `order_retraction_candidate.csv`，发布后调用现有撤销发布 API 并记录原因。迟到退款示例 `refund_late_arrival.csv` 的 sidecar `generated_at` 比 `business_date` 晚 40 天。

`boundary_cases/` 内的非法 ASIN、重复 record_id、非法币种、日期倒置和空报表是故意构造的拒绝/缺报夹具，不属于主数据。完整空退款报表只能在隔离验收来源确认 `empty_report_means_zero=true` 后使用；不得拿它覆盖已有事实。

## 许可证和来源

- 真实评分输入来自仓库已存在的 Amazon Reviews 2023 评论子集。上游仓库的 MIT `LICENSE` 是否覆盖 Amazon 评论内容并不明确；本包只输出聚合星级、ASIN/日期和评论关联 ID，公开或外发前仍要核实数据内容权利。[上游数据集卡](https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023) · [上游仓库 LICENSE](https://github.com/hyp1231/AmazonReviews2023/blob/main/LICENSE)
- 经营事实和客服模板是本项目生成的合成/派生测试数据；许可遵循本项目仓库规则。
- `manifest.json` 按文件记录 source、provenance、license、real/derived/synthetic 字段、版本、生成时间、覆盖范围和用途。`manifest.json` 也明确真实评论/黄金评论仍为原输入，不生成评论事实副本。

自动校验结果见 `validation/validation_report.json` 和 `validation/validation_results.csv`。它们说明本地数据构造满足结构及链接约束，不代表真实 Amazon 业务口径验收。Django contract regression 在 `backend/tests_v2/test_business_fact_package_contract.py`，会用项目真实 `field_catalog/parse_value` 解析主数据和边界导入文件。
