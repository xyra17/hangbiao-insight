import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";
import InvestigationEvidence from "./InvestigationEvidence";

describe("investigation evidence state boundaries", () => {
  it("shows partial work and order-tool gaps without claiming a completed conclusion", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="partial" result={{completion_status: "partial", subtasks: [
      {family: "daily", label: "销量与流量", mode: "metric_comparison", status: "facts_available", reason: "已读取冻结指标事实"},
      {family: "refunds", label: "退款", mode: "metric_comparison", status: "blocked", reason: "缺少退款来源"},
      {family: "order_records", label: "逐订单核查", mode: "order_lookup", status: "unsupported", reason: "不提供订单明细读取"},
    ]}} />);
    expect(html).toContain("部分核查结束，仍有待补事项");
    expect(html).toContain("销量与流量：冻结事实可用");
    expect(html).toContain("退款：暂停比较");
    expect(html).toContain("缺少授权核查工具");
    expect(html).toContain("结论未知");
  });
  it("retains available daily denominators when a blocked refund atom is cited", () => {
    const atom = {type: "metric" as const, snapshot_id: "s1", snapshot_fingerprint: "f1", source_manifest_sha256: "m1", shop_id: "shop", child_asin: "asin", path: "cohort.refunds.status", window: "snapshot", value: "missing", unit: "fact"};
    const html = renderToStaticMarkup(<InvestigationEvidence runId="available-facts" result={{completion_status: "partial", subtasks: [
      {family: "daily", label: "销量与流量", mode: "metric_comparison", status: "facts_available", reason: "冻结事实可用"},
      {family: "refunds", label: "退款", mode: "metric_comparison", status: "blocked", reason: "缺源"},
    ], business_basis: {citations: [atom]}, metric_evidence: [{identity: {snapshot_id: "s1", child_asin: "asin"}, catalog_complete: true, atoms: [atom,
      {...atom, path: "daily.current.unit_session_percent", window: "current", start: "2026-09-01", end: "2026-09-07", value: "4.0", unit: "percent", denominator: "200"},
    ]}]}} />);
    expect(html).toContain("分母：200 会话");
    expect(html).toContain("当前窗");
    expect(html).toContain("缺少来源或数据");
  });
  it("renders metric denominators without comments or causal confirmation", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="controlled" result={{metric_evidence: [{identity: {snapshot_id: "snapshot-1", child_asin: "B000TEST01"}, catalog_complete: true, atoms: [{type: "metric", snapshot_id: "snapshot-1", snapshot_fingerprint: "fingerprint", source_manifest_sha256: "source", shop_id: "shop", child_asin: "B000TEST01", path: "daily.current.unit_session_percent", window: "current", start: "2026-09-01", end: "2026-09-07", value: "4.0", unit: "percent", denominator: "200"}]}]}} />);
    expect(html).toContain("冻结指标证据");
    expect(html).toContain("分母：200 会话");
    expect(html).toContain("结论未知");
    expect(html).toContain("指标事实不能单独证明经营原因");
  });
  it("marks unavailable semantic judgment as incomplete instead of no answer", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="failed-judgment" result={{relevance_judgments: [{complete: false, status: "incomplete", error: "provider_unavailable"}]}} />);
    expect(html).toContain("语义相关性判定未完成");
    expect(html).toContain("不能作为确定无答案");
  });
  it("keeps legacy missing structured evidence unknown", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="legacy" result={{}} />);
    expect(html).toContain("历史调查未记录结构化结论");
    expect(html).not.toContain("冻结指标证据");
  });
  it("keeps title and body contexts with identical local offsets distinct", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="title-context" result={{observations: [{fact_id: "f1", record_id: "r1", title: "Damaged", text: "No damage", match_kind: "relevant_hit", evidence_contexts: [
      {source_field: "title", start: 0, end: 7, text: "Title assertion", context: "asserted"},
      {source_field: "text", start: 0, end: 7, text: "Body denial", context: "negated"},
    ]}]}} />);
    expect(html).toContain("标题语境");
    expect(html).toContain("Title assertion");
    expect(html).toContain("Body denial");
  });

  it("separates occurrence amounts by currency without a mature-order denominator", () => {
    const atom = {type: "metric" as const, snapshot_id: "s", snapshot_fingerprint: "f", source_manifest_sha256: "m", shop_id: "shop", child_asin: "asin", path: "occurrence.refunds.current.amount_by_currency.USD", window: "current", start: "2026-10-01", end: "2026-10-07", value: "25.00", unit: "USD"};
    const html = renderToStaticMarkup(<InvestigationEvidence runId="occurrence" result={{completion_status: "partial", subtasks: [
      {family: "refunds", label: "退款发生口径", mode: "occurrence", fact_prefix: "occurrence.refunds", status: "facts_available", reason: "按币种分列"},
      {family: "refunds", label: "退款成熟队列", mode: "cohort", status: "blocked", reason: "缺订单来源"},
    ], metric_evidence: [{identity: {snapshot_id: "s", child_asin: "asin"}, catalog_complete: true, atoms: [atom, {...atom, path: "occurrence.refunds.current.amount_by_currency.EUR", value: "8.00", unit: "EUR"}]}]}} />);
    expect(html).toContain("退款发生金额：25.00 USD");
    expect(html).toContain("退款发生金额：8.00 EUR");
    expect(html).toContain("2026-10-01");
    expect(html).not.toContain("分母：");
    expect(html).toContain("退款成熟队列：暂停比较");
  });

  it("shows requested and available dates without displaying wrong-window amounts", () => {
    const atom = {type: "metric" as const, snapshot_id: "s", snapshot_fingerprint: "f", source_manifest_sha256: "m", shop_id: "shop", child_asin: "asin", path: "occurrence.refunds.current.amount_by_currency.USD", window: "current", start: "2026-09-15", end: "2026-09-28", value: "999.00", unit: "USD"};
    const html = renderToStaticMarkup(<InvestigationEvidence runId="mismatch" result={{request_scope: {status: "explicit", start: "2025-01-01", end: "2025-01-31", anchor_date: "2026-10-08", timezone: "America/Los_Angeles", reason: "不替换为默认两窗"}, subtasks: [{family: "refunds", label: "退款", mode: "occurrence", status: "blocked", reason: "请求窗口不一致", actual_windows: [{window: "current", start: "2026-09-15", end: "2026-09-28"}]}], metric_evidence: [{identity: {snapshot_id: "s", child_asin: "asin"}, catalog_complete: true, atoms: [atom]}]}} />);
    expect(html).toContain("请求日期范围");
    expect(html).toContain("2025-01-31");
    expect(html).toContain("实际可用窗口");
    expect(html).toContain("2026-09-28");
    expect(html).not.toContain("999.00");
  });

  it("distinguishes historical order associations and unresolved links", () => {
    const html = renderToStaticMarkup(<InvestigationEvidence runId="links" result={{evidence: [{type: "order_records", scope: {start: "2026-09-01", end: "2026-09-30"}, associations: {requested_order_line_ids: ["L1", "L2"], unresolved_order_line_ids: ["L2"]}, sections: {orders: {status: "sampled", complete: false, items: [{fact_id: "f1", raw_row_id: "r1", business_date: "2026-08-31", record_id: "L1", association_only: true}]}}}]}} />);
    expect(html).toContain("仅历史关联补读");
    expect(html).toContain("未解析关联：1 条");
    expect(html).toContain("关联尚未补齐");
    expect(html).toContain("达到样本上限");
    expect(html).not.toContain("限定范围已读取");
  });

});
