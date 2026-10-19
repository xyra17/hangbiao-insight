import { Alert, Card, Empty, Space, Tag, Typography } from "antd";

type Observation = {
  fact_id?: string;
  record_id?: string;
  raw_row_id?: string;
  business_date?: string;
  revision?: number;
  title?: string;
  text?: string;
  match_kind?: string;
  evidence_relation?: string;
  source_kind?: string;
  phase?: string;
  evidence_contexts?: { source_field?: string; start: number; end: number; text: string; context: string }[];
};

type MetricAtom = {
  type: "metric"; snapshot_id: string; snapshot_fingerprint: string; source_manifest_sha256: string;
  shop_id: string; child_asin: string; product_id?: string | null; path: string; window: string;
  start?: string | null; end?: string | null; value: string; unit: string; denominator?: string | null;
};
const metricLabels: Record<string, string> = {status: "状态", reason: "依据", denominator: "成熟订单分母", numerator: "发生订单数", rate: "订单比例", sessions: "会话总量", ordered_units: "销售件数总量", unit_session_percent: "件数／会话百分数", currency: "币种", sales_amount: "销售额", event_basis: "发生口径", event_count: "发生记录数", reversal_count: "冲正记录数", units: "发生件数", observing_orders: "观察中订单", immature_orders: "未成熟订单", mature_orders: "成熟订单", chain_status: "交易链状态"};
const metricValues: Record<string, string> = {ready: "已就绪", missing: "缺少来源或数据", partial: "覆盖不完整", stale: "数据过期", invalid: "口径不符", low_sample: "样本不足", requested: "退货申请", received: "仓库收到", reversal_without_positive_anchor: "冲正缺原正向交易", null: "未知"};
const metricUnits: Record<string, string> = {fraction: "比例（0—1）", percent: "%", percentage_point: "百分点", orders: "订单", records: "条", sessions: "会话", units: "件", days: "天", stars: "星", fact: "状态事实", unknown_currency: "币种未知"};

type RequestScope = { status: string; timezone?: string; anchor_date?: string; start?: string | null; end?: string | null; reason?: string };
export type InvestigationEvidenceResult = {
  request_scope?: RequestScope | null;
  completion_status?: string;
  subtasks?: { family: string; label: string; mode: string; status: string; reason: string; fact_prefix?: string; requested_scope?: RequestScope; actual_windows?: {window: string; start?: string | null; end?: string | null}[] }[];
  business_basis?: { origin?: string; instruction?: string; citations?: MetricAtom[]; allowed_statement?: string; forbidden_statement?: string; stop_condition?: string } | null;
  metric_evidence?: { provenance?: string; identity: { snapshot_id: string; child_asin: string }; atoms: MetricAtom[]; catalog_complete: boolean }[];
  relevance_judgments?: { status?: string; complete?: boolean; error?: string; cost_known?: boolean }[];
  conclusion?: { status?: string; statement?: string; proposed_statement?: string; citation_status?: string; citations?: ({fact_id: string; quote: string} | MetricAtom)[] };
  retrieval_summary?: { related_searches?: number; context_searches?: number; empty_searches?: number; incomplete_searches?: number };
  observations?: Observation[];
  evidence?: { type?: string; associations?: { requested_order_line_ids?: string[]; unresolved_order_line_ids?: string[] }; items?: Observation[]; scope?: { kind?: string; start?: string; end?: string }; phase?: string; sections?: Record<string, {status: string; complete: boolean; source_id?: string; revision?: number; items: {fact_id: string; raw_row_id: string; business_date: string; record_id?: string; order_line_id?: string; order_id?: string; amount?: string; currency?: string; quantity?: number | null; status?: string; shipped_date?: string | null; requested_date?: string | null; received_date?: string | null; association_only?: boolean}[]}> }[];
  limitations?: string[];
};

export default function InvestigationEvidence({ result, runId }: { result: InvestigationEvidenceResult; runId: string }) {
  const observations = result.observations ?? (result.evidence ?? []).flatMap((entry) =>
    entry.type === "text_retrieval" ? (entry.items ?? []).map((item) => ({ ...item, source_kind: entry.scope?.kind, phase: entry.phase })) : [],
  );
  const summary = result.retrieval_summary;
  const citedMetrics = [...(result.conclusion?.citations ?? []).filter((item): item is MetricAtom => "type" in item && item.type === "metric"), ...(result.business_basis?.citations ?? [])];
  const availablePrefixes = (result.subtasks ?? []).filter((task) => task.status === "facts_available")
    .map((task) => task.fact_prefix ?? (task.family === "daily" ? "daily" : `${task.mode === "occurrence" ? "occurrence" : "cohort"}.${task.family}`));
  const displayFact = (atom: MetricAtom) => /\.(status|rate|denominator|sessions|unit_session_percent|event_count|reversal_count|units|event_basis)$/.test(atom.path) || atom.path.includes(".amount_by_currency.");
  const availableFacts = (result.metric_evidence ?? []).flatMap((entry) => entry.atoms.filter((atom) =>
    availablePrefixes.some((prefix) => atom.path.startsWith(`${prefix}.`)) && displayFact(atom) && (result.request_scope?.status !== "explicit" || atom.path.endsWith(".status") ||
      !!atom.start && !!atom.end && !!result.request_scope.start && !!result.request_scope.end && result.request_scope.start <= atom.start && atom.end <= result.request_scope.end),
  ));
  const metricAtoms = citedMetrics.length || result.subtasks?.length ? [...citedMetrics, ...availableFacts] : (result.metric_evidence ?? []).flatMap((entry) => entry.atoms.filter(displayFact)).slice(0, 12);
  const uniqueMetrics = [...new Map(metricAtoms.map((atom) => [`${atom.snapshot_id}:${atom.path}`, atom])).values()];
  const unique = [...new Map(observations.map((item) => [`${item.fact_id}:${item.match_kind}`, item])).values()];
  return (
    <Card title="结论与证据边界" data-testid="investigation-evidence" data-run-id={runId}>
      <Space orientation="vertical" style={{ width: "100%" }} size="middle">
        <Alert type="info" showIcon title={result.conclusion?.status === "pending_adjudication" ? "引用已核查，结论仍待人工判断" : "结论未知"}
          description={result.conclusion?.statement ?? "历史调查未记录结构化结论，不能据此视为已确认。"} />
        {result.request_scope && <Alert type={result.request_scope.status === "needs_clarification" ? "warning" : "info"} showIcon title="请求日期范围" data-testid="investigation-request-scope"
          description={<><div>{result.request_scope.start && result.request_scope.end ? `${result.request_scope.start} — ${result.request_scope.end}` : "等待明确起止日期"} · 店铺时区：{result.request_scope.timezone ?? "未提供"} · 解析锚点：{result.request_scope.anchor_date ?? "未提供"}</div><div>{result.request_scope.reason}</div></>} />}
        {!!result.subtasks?.length && <Alert type={result.completion_status === "partial" || result.completion_status === "blocked" ? "warning" : "info"}
          showIcon title={result.completion_status === "partial" ? "部分核查结束，仍有待补事项" : result.completion_status === "blocked" ? "当前请求仍有前置缺口" : "已读取指标事实，结论仍待核查"}
          description={<ul>{result.subtasks.map((task) => <li key={`${task.family}:${task.mode}`}>
            <Typography.Text strong>{task.label}：{task.status === "unsupported" ? "缺少授权核查工具" : task.status === "blocked" ? "暂停比较" : "冻结事实可用"}</Typography.Text>
            <div>{task.reason}</div>
            {!!task.actual_windows?.length && <div>实际可用窗口：{task.actual_windows.map((window) => `${window.window === "baseline" ? "基准窗" : window.window === "current" ? "当前窗" : "事件窗"} ${window.start ?? "未知"} — ${window.end ?? "未知"}`).join("；")}</div>}
          </li>)}</ul>} data-testid="investigation-subtasks" />}
        {result.conclusion?.proposed_statement && (
          <Typography.Paragraph>模型提议（尚未确认）：{result.conclusion.proposed_statement}</Typography.Paragraph>
        )}
        {result.business_basis?.origin === "deterministic_tool_facts" && result.business_basis.instruction && (
          <Alert type="info" title="下一步核查建议" description={result.business_basis.instruction} />
        )}
        {(result.relevance_judgments ?? []).some((entry) => entry.complete === false) && <Alert type="warning" title="语义相关性判定未完成" description="模型不可用、预算限制或部分候选无法判断；当前结果不能作为确定无答案。默认词法检索仍可单独使用。" />}
        {uniqueMetrics.length > 0 && <Card size="small" title="冻结指标证据" data-testid="metric-evidence">
          <Typography.Paragraph type="secondary">引用已绑定本次冻结对象和来源版本；指标事实不能单独证明经营原因。</Typography.Paragraph>
          {(result.metric_evidence ?? []).filter((entry) => entry.provenance?.includes("SYNTHETIC")).map((entry) => <Tag key={entry.identity.snapshot_id} color="gold">{entry.provenance}</Tag>)}
          {uniqueMetrics.map((atom) => <Card size="small" key={`${atom.snapshot_id}:${atom.path}`} style={{ marginBottom: 8, overflowWrap: "anywhere" }}>
            <Typography.Text strong>{atom.path.includes(".amount_by_currency.") ? "退款发生金额" : metricLabels[atom.path.split(".").at(-1)!] ?? "指标事实"}：{metricValues[atom.value] ?? atom.value} {metricUnits[atom.unit] ?? atom.unit}</Typography.Text>
            <Typography.Paragraph>窗口：{atom.window === "baseline" ? "基准窗" : atom.window === "current" ? "当前窗" : "快照状态"} · {atom.start ?? "起日未提供"} — {atom.end ?? "止日未提供"}</Typography.Paragraph>
            {atom.denominator != null && <Typography.Paragraph>分母：{atom.denominator} {atom.path.includes("unit_session_percent") ? "会话" : "订单"}</Typography.Paragraph>}
            <Typography.Paragraph type="secondary">对象：{atom.child_asin}{atom.product_id ? ` / SKU对象 ${atom.product_id}` : " / ASIN汇总"}；快照：{atom.snapshot_id}</Typography.Paragraph>
            <Typography.Paragraph type="secondary">指标路径：{atom.path}；来源指纹：{atom.source_manifest_sha256.slice(0, 12)}；快照指纹：{atom.snapshot_fingerprint.slice(0, 12)}</Typography.Paragraph>
          </Card>)}
          <Typography.Text type="secondary">仅展示本次引用或主要状态，不代表列出全部指标。未知字段不补零。</Typography.Text>
        </Card>}
        {(result.evidence ?? []).filter((entry) => entry.type === "order_records").map((entry, index) => <Card size="small" key={`orders:${index}`} title="授权冻结订单记录" data-testid="order-records-evidence">
          <Typography.Paragraph type="secondary">每类来源最多20条；仅核对限定窗口及关联样本。未达到上限不表示来源覆盖完整，不据此推断交易链完整性或总体风险。</Typography.Paragraph>
          <Typography.Paragraph>请求事件窗：{entry.scope?.start ?? "未知"} — {entry.scope?.end ?? "未知"}；关联补读的历史订单单独标记，不计入当期事件数量。</Typography.Paragraph>
          {entry.associations && <Typography.Paragraph>样本引用订单：{entry.associations.requested_order_line_ids?.length ?? 0} 条；未解析关联：{entry.associations.unresolved_order_line_ids?.length ?? 0} 条。{(entry.associations.unresolved_order_line_ids?.length ?? 0) > 0 && "关联尚未补齐，需补源或缩小窗口。"}</Typography.Paragraph>}
          {Object.entries(entry.sections ?? {}).map(([kind, section]) => <div key={kind}>
            <Typography.Text strong>{({orders: "订单与发货", refunds: "退款交易", returns: "退货记录"} as Record<string, string>)[kind] ?? kind}：{section.status === "missing" ? "缺少来源" : section.status === "unresolved" ? "引用订单未解析" : section.complete ? "限定范围已读取" : "达到样本上限"}</Typography.Text>
            {section.items.map((item) => <Card size="small" key={item.fact_id} style={{marginTop: 8, marginBottom: 8, overflowWrap: "anywhere"}}>
              <Typography.Paragraph>{item.association_only && <Tag color="gold">仅历史关联补读</Tag>}记录：{item.record_id ?? "未知"} · 发生日：{item.business_date}</Typography.Paragraph>
              <Typography.Paragraph>订单：{item.order_id ?? item.order_line_id ?? "未知"}；数量：{item.quantity ?? "未知"} 件{item.amount != null ? `；交易金额：${item.amount} ${item.currency ?? "币种未知"}` : ""}</Typography.Paragraph>
              <Typography.Paragraph>状态：{item.status ? metricValues[item.status] ?? item.status : "未知"}；发货日：{item.shipped_date ?? "未知"}；申请日：{item.requested_date ?? "未知"}；收到日：{item.received_date ?? "未知"}</Typography.Paragraph>
              <Typography.Paragraph type="secondary">来源：{section.source_id ?? "未知"} · 版本：{section.revision ?? "未知"} · 原始行：{item.raw_row_id} · 事实：{item.fact_id}</Typography.Paragraph>
            </Card>)}
          </div>)}
        </Card>)}
        {summary && <Space wrap>
          <Tag color="cyan">相关命中：{summary.related_searches ?? 0} 次检索</Tag>
          <Tag color="gold">仅范围上下文：{summary.context_searches ?? 0} 次检索</Tag>
          {!!summary.incomplete_searches && <Tag color="orange">检索未完成：{summary.incomplete_searches} 次；不能断言无证据</Tag>}
          <Tag>未找到相关证据：{summary.empty_searches ?? 0} 次检索</Tag>
        </Space>}
        {!unique.length && <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="当前调查没有可展示的原文证据；这不代表问题不存在。" />}
        {unique.map((item, index) => <Card size="small" key={`${item.fact_id}:${item.match_kind}:${index}`} style={{ width: "100%", overflowWrap: "anywhere" }}>
          <Space wrap>
            <Tag color={item.match_kind === "relevant_hit" ? "cyan" : item.match_kind === "scope_context" ? "gold" : undefined}>
              {item.match_kind === "relevant_hit" ? "相关命中" : item.match_kind === "scope_context" ? "仅范围上下文" : "历史证据：相关性未标记"}
            </Tag>
            <Typography.Text strong>{item.title || item.record_id || "原文记录"}</Typography.Text>
          </Space>
          <Typography.Paragraph style={{ whiteSpace: "pre-wrap", marginTop: 12 }}>{item.text}</Typography.Paragraph>
          <Typography.Paragraph type="secondary">
            {item.source_kind === "support" ? "售后工单" : item.source_kind === "reviews" ? "评论" : "原文"}
            {" · "}{item.business_date ?? "日期未记录"}{" · 来源版本 "}{item.revision ?? "未记录"}
          </Typography.Paragraph>
          <Typography.Paragraph type="secondary">支持/反证性质：{item.evidence_relation === "support" ? "支持" : item.evidence_relation === "counter" ? "反证" : "待判断"}；
            {item.phase === "counter_evidence" ? "反证方向检索不代表已形成反证。" : "主题命中不代表结论已成立。"}</Typography.Paragraph>
          {(item.evidence_contexts ?? []).filter((context, i, all) => all.findIndex((other) => (other.source_field ?? "text") === (context.source_field ?? "text") && other.start === context.start && other.end === context.end) === i).map((context) => (
            <Typography.Paragraph key={`${context.source_field ?? "text"}:${context.start}:${context.end}`} type="secondary">{context.source_field === "title" ? "标题语境" : "原文语境"}（{context.context === "asserted" ? "肯定陈述" : context.context === "negated" ? "否定陈述" : context.context === "hypothetical" ? "条件假设" : "待核查"}）：{context.text}</Typography.Paragraph>
          ))}
          {item.raw_row_id && <Typography.Text type="secondary">原始行：{item.raw_row_id}</Typography.Text>}
        </Card>)}
        <ul>{(result.limitations ?? []).map((item) => <li key={item}>{item}</li>)}</ul>
      </Space>
    </Card>
  );
}
