import { FormEvent, useEffect, useMemo, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  BarChart3,
  Bell,
  Bot,
  Boxes,
  CalendarDays,
  ChevronDown,
  CircleHelp,
  ClipboardCheck,
  Database,
  Download,
  ExternalLink,
  FileSearch,
  Gauge,
  LayoutDashboard,
  Lightbulb,
  Moon,
  PackageSearch,
  RefreshCw,
  Search,
  Settings,
  ShieldCheck,
  ShoppingBag,
  Sparkles,
  Star,
  Store,
  TrendingDown,
  TrendingUp,
  X,
  Zap,
} from "lucide-react";
import { TopicChart, TrendChart } from "./components/Charts";
import { API_BASE, api, apiPost, apiUpload, type Anomaly, type OverviewData, type Review, type ReviewSearchMeta, type Topic, type ReviewSignalCompare } from "./lib/api";

type Page = "overview" | "products" | "anomalies" | "investigation" | "reviews" | "sources";

const nav = [
  { id: "overview" as const, label: "经营总览", detail: "核心指标与风险", icon: LayoutDashboard },
  { id: "products" as const, label: "商品资源库", detail: "SKU 与商品证据", icon: ShoppingBag },
  { id: "anomalies" as const, label: "异常监控流", detail: "优先处理队列", icon: AlertTriangle },
  { id: "reviews" as const, label: "语义洞察", detail: "主题与评论原文", icon: FileSearch },
  { id: "sources" as const, label: "数据总线", detail: "导入与连接", icon: Database },
];

const pageMeta: Record<Page, { label: string; detail: string }> = {
  overview: { label: "经营总览", detail: "核心指标与风险" },
  products: { label: "商品资源库", detail: "SKU 与商品证据" },
  anomalies: { label: "异常监控流", detail: "优先处理队列" },
  investigation: { label: "AI 深度诊断", detail: "调查与证据" },
  reviews: { label: "语义洞察", detail: "主题与评论原文" },
  sources: { label: "数据总线", detail: "导入与连接" },
};

const iconByKpi = { orders: Boxes, refund: RefreshCw, rating: Star, reviews: MessageIcon };

function MessageIcon(props: { size?: number }) {
  return <Activity {...props} />;
}

function formatValue(value: number, unit: string) {
  if (unit === "单" || unit === "条") return Math.round(value).toLocaleString("zh-CN");
  return value.toFixed(unit === "%" ? 2 : 1);
}

/** 月份偏移：从接口返回的最新周期推导基线周期，周期不得硬编码（V2 第 8.3 节）。 */
function shiftMonth(period: string, offset: number) {
  const match = /^(\d{4})-(\d{2})$/.exec(period);
  if (!match) return "";
  const date = new Date(Number(match[1]), Number(match[2]) - 1 + offset, 1);
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}`;
}

export default function App() {
  const [page, setPage] = useState<Page>("overview");
  const [selectedProduct, setSelectedProduct] = useState("B07P9S6R9Y");
  const [overview, setOverview] = useState<OverviewData | null>(null);
  const [topics, setTopics] = useState<Topic[]>([]);
  const [signalCompare, setSignalCompare] = useState<ReviewSignalCompare | null>(null);
  const [reviews, setReviews] = useState<Review[]>([]);
  const [reviewMeta, setReviewMeta] = useState<ReviewSearchMeta>({ retrieval_backend: "keyword", retrieval_version: "keyword-v1", embedding_version: null, rerank_mode: "never" });
  const [investigation, setInvestigation] = useState<any>(null);
  const [selectedAnomaly, setSelectedAnomaly] = useState<Anomaly | null>(null);
  const [selectedReview, setSelectedReview] = useState<Review | null>(null);
  const [searchQuery, setSearchQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [dark, setDark] = useState(false);

  useEffect(() => {
    document.documentElement.classList.toggle("dark", dark);
  }, [dark]);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    Promise.all([
      api<OverviewData>(`/api/v1/overview?product_id=${selectedProduct}`),
      api<{ items: Topic[] }>(`/api/v1/reviews/topics?product_id=${selectedProduct}`),
      api<{ items: Review[] } & ReviewSearchMeta>(`/api/v1/reviews/search?product_id=${selectedProduct}&limit=6`),
    ])
      .then(([overviewData, topicData, reviewData]) => {
        if (!active) return;
        setOverview(overviewData);
        setTopics(topicData.items);
        setReviews(reviewData.items);
        setReviewMeta(reviewData);
      })
      .catch((reason) => active && setError(reason instanceof Error ? reason.message : "数据加载失败"))
      .finally(() => active && setLoading(false));
    return () => {
      active = false;
    };
  }, [selectedProduct]);

  // 周期由接口数据推导（V2 第 8.3 节：周期不得硬编码）。
  // 优先取该商品的最新异常月份——主题/极性对比本就是为解释异常窗口服务的；
  // 无异常时才回退到趋势最后一个点。
  // 注意不能直接取趋势最新月：数据尾部评论量枯竭（如 2023-08 仅 5 条评论），
  // 会让对比面板长期落在「样本不足」。
  const currentPeriod = useMemo(() => {
    const anomalyMonths = (overview?.anomalies ?? [])
      .map((item) => item.month)
      .filter(Boolean)
      .sort();
    if (anomalyMonths.length) return anomalyMonths[anomalyMonths.length - 1];
    const trendMonths = (overview?.trend?.points ?? [])
      .map((point) => point.month)
      .filter(Boolean)
      .sort();
    return trendMonths[trendMonths.length - 1] ?? "";
  }, [overview]);
  const baselinePeriod = useMemo(() => shiftMonth(currentPeriod, -1), [currentPeriod]);

  // 主题/极性对比依赖已推导的周期，故独立于首屏请求，待周期就绪后再取。
  useEffect(() => {
    if (!currentPeriod || !baselinePeriod) {
      setSignalCompare(null);
      return;
    }
    let active = true;
    api<ReviewSignalCompare>(`/api/v1/reviews/signals/compare?product_id=${selectedProduct}&current_period=${currentPeriod}&baseline_period=${baselinePeriod}`)
      .then((data) => active && setSignalCompare(data))
      .catch(() => active && setSignalCompare(null));
    return () => {
      active = false;
    };
  }, [selectedProduct, currentPeriod, baselinePeriod]);

  const product = overview?.products.find((item) => item.product_id === selectedProduct);
  const currentPage = pageMeta[page];
  const highRiskCount = overview?.anomalies.filter((item) => item.severity === "high").length ?? 0;

  async function openInvestigation(anomaly?: Anomaly, followUpQuery?: string, parentInvestigationId?: string) {
    const current = anomaly ?? selectedAnomaly ?? overview?.anomalies[0];
    setSelectedAnomaly(current ?? null);
    setPage("investigation");
    setInvestigation(null);
    try {
      const created = await apiPost<{ investigation_id: string }>("/api/v1/investigations", { user_query: followUpQuery ?? current?.summary ?? "请诊断当前商品经营异常并查找评论证据", product_id: current?.product_id ?? selectedProduct, period: current?.month ?? currentPeriod, parent_investigation_id: parentInvestigationId });
      setInvestigation({ status: "pending", request_id: created.investigation_id, observations: [], actions: [], evidence: [], hypotheses: [], conflicts: [], trace: [] });
      const source = new EventSource(`${API_BASE}/api/v1/investigations/${created.investigation_id}/stream`);
      source.onmessage = async () => {
        try { setInvestigation(await api(`/api/v1/investigations/${created.investigation_id}`)); } catch { /* reconnect handled by browser */ }
      };
      source.onerror = () => source.close();
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : "调查加载失败");
    }
  }

  async function runSearch(event: FormEvent) {
    event.preventDefault();
    const result = await api<{ items: Review[] } & ReviewSearchMeta>(
      `/api/v1/reviews/search?product_id=${selectedProduct}&q=${encodeURIComponent(searchQuery)}&limit=10`,
    );
    setReviews(result.items);
    setReviewMeta(result);
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark"><Gauge size={22} /></div>
          <div><strong>INSIGHT</strong><span>跨境商品经营洞察</span></div>
        </div>
        <nav>
          {nav.map((item) => (
            <button key={item.id} className={page === item.id ? "active" : ""} onClick={() => setPage(item.id)}>
              <item.icon size={18} />
              <span><strong>{item.label}</strong><small>{item.detail}</small></span>
              {item.id === "anomalies" && highRiskCount > 0 && <b>{highRiskCount}</b>}
            </button>
          ))}
          <button className="disabled"><BarChart3 size={18} /><span><strong>广告链路分析</strong><small>数据源待接入</small></span><em>待接入</em></button>
        </nav>
        <div className="sidebar-bottom">
          <button disabled title="帮助中心尚未接入"><CircleHelp size={18} />帮助与口径（待接入）</button>
          <button onClick={() => setPage("sources")}><Settings size={18} />系统设置</button>
          <div className="profile"><span>OP</span><div><strong>运营总监</strong><small>PRO 授权版</small></div><i /></div>
        </div>
      </aside>

      <main>
        <header className="topbar">
          <div className="page-title"><small>INSIGHT / OPERATIONS</small><strong>{currentPage.label}</strong></div>
          <div className="top-actions">
            <span className="live-pill"><i />系统在线</span>
            <button className="lang-button" title="语言切换待接入">EN</button>
            <span className="timezone">北京时间</span>
            <button className="icon-button" onClick={() => setDark((value) => !value)} title="切换明暗主题"><Moon size={16} /></button>
          </div>
        </header>

        <div className="page-wrap">
          {error && <div className="error-banner"><AlertTriangle size={17} />{error}<span>请确认后端已在 8200 端口启动</span></div>}
          <AnimatePresence mode="wait">
            <motion.div key={page} initial={{ opacity: 0, y: 9 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0, y: -6 }} transition={{ duration: 0.28 }}>
              {page === "overview" && <OverviewPage data={overview} loading={loading} productId={selectedProduct} setProductId={setSelectedProduct} topics={topics} signalCompare={signalCompare} onInvestigate={openInvestigation} />}
              {page === "products" && <ProductsPage data={overview} selected={selectedProduct} onSelect={(id) => { setSelectedProduct(id); setPage("overview"); }} />}
              {page === "anomalies" && <AnomaliesPage anomalies={overview?.anomalies ?? []} onInvestigate={openInvestigation} />}
              {page === "investigation" && <InvestigationPage data={investigation} onEvidence={setSelectedReview} onStart={() => openInvestigation()} onFollowUp={(query) => openInvestigation(selectedAnomaly ?? undefined, query, investigation?.request_id)} />}
              {page === "reviews" && <ReviewsPage topics={topics} signalCompare={signalCompare} reviews={reviews} reviewMeta={reviewMeta} query={searchQuery} setQuery={setSearchQuery} onSearch={runSearch} onReview={setSelectedReview} />}
              {page === "sources" && <SourcesPage />}
            </motion.div>
          </AnimatePresence>
        </div>
      </main>

      <AnimatePresence>{selectedReview && <ReviewDrawer review={selectedReview} onClose={() => setSelectedReview(null)} />}</AnimatePresence>
    </div>
  );
}

function ProductScope({ data, productId, setProductId }: { data: OverviewData; productId: string; setProductId: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  const current = data.products.find((product) => product.product_id === productId);
  return <div className="scope-product">
    <button className="scope-product-trigger" onClick={() => setOpen((value) => !value)} aria-expanded={open}>
      <span>全局观测域</span>
      <strong>{current?.name ?? "全部观测商品"}</strong>
      <ChevronDown size={16} />
    </button>
    {open && <div className="scope-product-menu">
      {data.products.map((product) => <button key={product.product_id} className={product.product_id === productId ? "active" : ""} onClick={() => { setProductId(product.product_id); setOpen(false); }}>
        <span>{product.name}</span><small>{product.product_id} · {product.marketplace}</small>
      </button>)}
    </div>}
  </div>;
}

function AlertCard({ anomaly, onInvestigate }: { anomaly: Anomaly; onInvestigate: (item: Anomaly) => void }) {
  return <article className="index-alert">
    <span className="index-alert-badge">极高风险</span>
    <h3>{anomaly.summary}</h3>
    <p>{anomaly.product_name} · {anomaly.month}<br />{anomaly.signals.map((signal) => `${signal.label} ${signal.change > 0 ? "+" : ""}${signal.change}${signal.unit}`).join(" · ")}</p>
    <button onClick={() => onInvestigate(anomaly)}>启动诊断 <ArrowRight size={15} /></button>
  </article>;
}

function OverviewPage({ data, loading, productId, setProductId, topics, signalCompare, onInvestigate }: { data: OverviewData | null; loading: boolean; productId: string; setProductId: (id: string) => void; topics: Topic[]; signalCompare: ReviewSignalCompare | null; onInvestigate: (anomaly?: Anomaly) => void }) {
  const [months, setMonths] = useState(18);
  if (loading && !data) return <LoadingScreen />;
  if (!data) return null;
  const alert = data.anomalies.find((item) => item.severity === "high") ?? data.anomalies[0];
  const visiblePoints = data.trend.points.slice(-months);
  return <>
    <section className="scope-toolbar">
      <ProductScope data={data} productId={productId} setProductId={setProductId} />
      <div className="scope-time">
        <span className="scope-live"><i />实时监测</span>
        <span className="scope-updated">更新于 <b>{data.updated_at?.slice(11, 19) || "--:--:--"}</b></span>
        <label className="scope-range"><CalendarDays size={15} /><select value={months} onChange={(event) => setMonths(Number(event.target.value))}><option value={6}>最近 6 个月</option><option value={12}>最近 12 个月</option><option value={18}>最近 18 个月</option></select></label>
      </div>
    </section>
    <section className="kpi-grid">
      {data.kpis.map((kpi, index) => {
        const Icon = iconByKpi[kpi.key as keyof typeof iconByKpi] ?? Activity;
        return <motion.article className={`kpi-card ${kpi.tone}`} key={kpi.key} initial={{ opacity: 0, y: 16 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .06 }}>
          <div className="kpi-top"><span><Icon size={19} /></span><small>较上月</small></div>
          <div className="kpi-value"><strong>{formatValue(kpi.value, kpi.unit)}</strong><b>{kpi.unit}</b></div>
          <div className="kpi-footer"><span>{kpi.label}</span><em>{kpi.direction === "up" ? <TrendingUp size={13} /> : <TrendingDown size={13} />}{Math.abs(kpi.delta)}%</em></div>
        </motion.article>;
      })}
    </section>
    <section className="index-charts">
      <article className="panel trend-panel">
        <PanelHead icon={Activity} title="订单与退款趋势" subtitle="经营指标 · 月度" action={<div className="segmented"><button className={months === 6 ? "active" : ""} onClick={() => setMonths(6)}>6 月</button><button className={months === 12 ? "active" : ""} onClick={() => setMonths(12)}>12 月</button><button className={months === 18 ? "active" : ""} onClick={() => setMonths(18)}>18 月</button></div>} />
        <TrendChart points={visiblePoints} />
        <div className="chart-foot"><span><i className="dot purple" />经营指标</span><span><i className="dot amber" />已知异常窗口</span><small>数据版本：{data.trend.source_version}</small></div>
      </article>
      {alert ? <AlertCard anomaly={alert} onInvestigate={onInvestigate} /> : <Empty text="当前商品未发现已知异常" />}
    </section>
    <section className="index-bottom">
      <article className="panel anomaly-radar">
        <PanelHead icon={AlertTriangle} title="异常实体雷达" subtitle="按严重度与处理状态排序" action={<button className="text-button" onClick={() => onInvestigate(data.anomalies[0])}>进入诊断 <ArrowRight size={14} /></button>} />
        <table className="index-table">
          <thead><tr><th>目标实体</th><th>异常特征</th><th>捕获时间</th><th>威胁等级</th><th>处理状态</th></tr></thead>
          <tbody>{data.anomalies.slice(0, 4).map((item) => (
            <tr key={item.id} onClick={() => onInvestigate(item)}>
              <td><b>{item.product_name}</b><small>{item.product_id}</small></td>
              <td>{item.summary}</td>
              <td>{item.month}</td>
              <td><Severity value={item.severity} /></td>
              <td><i className="pending-dot" />待处理</td>
            </tr>
          ))}</tbody>
        </table>
        {!data.anomalies.length && <Empty text="当前商品未发现已知异常" />}
      </article>
      <article className="panel topic-panel">
        <PanelHead icon={FileSearch} title="评论信号结构" subtitle="Topic × Polarity · 离线语义层" action={<span className="verified"><ShieldCheck size={14} />Golden 不进入运行时</span>} />
        {topics.length ? <TopicChart topics={topics} /> : <Empty text="暂无业务 Prediction 信号或样本不足" />}
        {signalCompare && <div className="topic-meta"><span>当前 {signalCompare.current_period} · 基线 {signalCompare.baseline_period}</span><span>{signalCompare.insufficient_data ? "样本不足" : `${signalCompare.items.length} 个主题已完成对比`}</span></div>}
      </article>
    </section>
  </>;
}

function CopilotCard({ onStart, coverage }: { onStart: () => void; coverage: OverviewData["coverage"] }) {
  return <article className="copilot-card">
    <div className="copilot-orb"><Bot size={27} /><span /><i /></div>
    <div className="copilot-content"><span className="eyebrow light"><Zap size={12} />AI INVESTIGATION</span><h2>把异常交给证据驱动的调查。</h2><p>Agent 会核对指标、形成竞争假设、回查评论原文，并在证据不足时主动停止归因。</p><div className="prompt-chips"><button>为什么退款率上升？</button><button>订单上涨但退款也涨</button></div><button className="primary-button" onClick={onStart}><Sparkles size={16} />开始深度诊断 <ArrowRight size={16} /></button></div>
    <div className="coverage-strip"><span>待接证据源</span>{coverage.map((item) => <i key={item.name}>{item.name.replace(/ .*/, "")}</i>)}</div>
  </article>;
}

function ProductsPage({ data, selected, onSelect }: { data: OverviewData | null; selected: string; onSelect: (id: string) => void }) {
  return <>
    <SimpleLead eyebrow="PRODUCT CENTER" title="商品中心" description="集中查看 SKU 健康度、数据覆盖与异常数量。" />
    <div className="toolbar"><div className="search-box"><Search size={17} /><input placeholder="搜索商品名称或 ASIN" /></div><button className="primary-button small" disabled title="需要接入商品主数据源"><ShoppingBag size={16} />添加商品（待数据源）</button></div>
    <div className="product-grid">{data?.products.map((product, index) => <motion.button className={`product-card ${selected === product.product_id ? "selected" : ""}`} key={product.product_id} onClick={() => onSelect(product.product_id)} initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ delay: index * .05 }}>
      <div className="product-visual"><PackageSearch size={28} /><span>{product.marketplace}</span></div>
      <div className="product-meta"><span className={`status ${product.status}`}>{product.status === "attention" ? "需要关注" : "健康"}</span><small>{product.product_id}</small></div>
      <h3>{product.name}</h3><p>{product.category} · {product.source}</p>
      <div className="product-stats"><span><b>{product.avg_rating}</b>评分</span><span><b>{product.review_count.toLocaleString()}</b>评论</span><span><b>{product.anomaly_count}</b>异常</span></div>
      <div className="card-link">查看商品分析 <ArrowRight size={14} /></div>
    </motion.button>)}</div>
  </>;
}

function AnomaliesPage({ anomalies, onInvestigate }: { anomalies: Anomaly[]; onInvestigate: (item: Anomaly) => void }) {
  const [filter, setFilter] = useState("all");
  const filtered = filter === "all" ? anomalies : anomalies.filter((item) => item.severity === filter);
  return <>
    <SimpleLead eyebrow="ANOMALY COMMAND CENTER" title="异常中心" description="先处理高影响、可验证、仍待诊断的经营偏离。" />
    <div className="toolbar"><div className="segmented large"><button className={filter === "all" ? "active" : ""} onClick={() => setFilter("all")}>全部 {anomalies.length}</button><button className={filter === "high" ? "active" : ""} onClick={() => setFilter("high")}>高风险</button><button className={filter === "medium" ? "active" : ""} onClick={() => setFilter("medium")}>中风险</button></div><button className="ghost-button" onClick={() => downloadFile("anomalies.csv", ["id,product_id,month,severity,summary", ...filtered.map((x) => [x.id,x.product_id,x.month,x.severity,`\"${x.summary.replaceAll('"','""')}\"`].join(","))].join("\n"), "text/csv")}><Download size={16} />导出列表</button></div>
    <article className="panel anomaly-table"><div className="table-head"><span>异常与商品</span><span>信号变化</span><span>时间</span><span>风险</span><span>诊断状态</span><span /></div>{filtered.map((item) => <button className="table-row" key={item.id} onClick={() => onInvestigate(item)}><span><b>{item.summary}</b><small>{item.product_name} · {item.product_id}</small></span><span className="signal-pills">{item.signals.map((signal) => <i key={signal.label} className={signal.change > 0 ? "up" : "down"}>{signal.label} {signal.change > 0 ? "+" : ""}{signal.change}{signal.unit}</i>)}</span><span>{item.month}</span><span><Severity value={item.severity} /></span><span><i className="pending-dot" />待诊断</span><span><ArrowRight size={16} /></span></button>)}</article>
  </>;
}

function InvestigationPage({ data, onEvidence, onStart, onFollowUp }: { data: any; onEvidence: (review: Review) => void; onStart: () => void; onFollowUp: (query: string) => void }) {
  useEffect(() => { if (!data) onStart(); }, []); // eslint-disable-line react-hooks/exhaustive-deps
  if (!data) return <LoadingScreen text="正在准备调查工作台…" />;
  if (data.request_id) return <>
    <section className="investigation-head"><div><span className="mode-badge"><Sparkles size={13} />{data.execution_mode ?? "REALTIME AGENT"}</span><h1>商品经营异常调查</h1><p>{data.product_id} · {data.period} · 状态：{data.status}{data.parent_investigation_id ? ` · 续自 ${data.parent_investigation_id}` : ""}</p></div><div className="investigation-actions"><button className="ghost-button" onClick={() => downloadFile(`investigation-${data.request_id}.md`, investigationMarkdown(data), "text/markdown")}><Download size={16} />导出报告</button><button className="primary-button small" onClick={() => { const q = window.prompt("请输入后续问题", "继续核对负向评论与退款异常的关系"); if (q?.trim()) onFollowUp(q.trim()); }}><Bot size={16} />继续追问</button></div></section>
    <section className="investigation-grid"><article className="panel timeline-panel"><PanelHead icon={Activity} title="调查轨迹" subtitle="实时后端事件 · 可审计" /><div className="timeline">{(data.trace ?? []).map((event: any, index: number) => <motion.div className="timeline-step done" key={`${event.timestamp}-${index}`} initial={{ opacity: 0 }} animate={{ opacity: 1 }}><span>{index + 1}</span><div><small>{event.event_type}</small><strong>{event.event_type === "tool_completed" ? "工具完成" : event.event_type === "planner_decision" ? "规划决策" : "调查事件"}</strong><p>{JSON.stringify(event.detail ?? {})}</p></div></motion.div>)}</div></article><div className="investigation-main"><article className="diagnosis-card"><div className="diagnosis-icon"><ShieldCheck size={23} /></div><div><span>调查状态</span><h2>{data.status === "running" ? "正在调查…" : data.status === "completed" ? "调查完成" : data.stop_reason ?? "等待调查"}</h2><p>{data.observations?.length ?? 0} 个观察 · {data.evidence?.length ?? 0} 条已验证证据</p></div></article><article className="panel"><PanelHead icon={FileSearch} title="证据 Evidence" subtitle="仅展示通过 Guard 的证据" />{data.evidence?.length ? <pre>{JSON.stringify(data.evidence, null, 2)}</pre> : <Empty text="尚未形成可验证证据" />}</article></div><aside className="missing-panel"><span className="eyebrow"><Database size={12} />调查上下文</span><h3>工具观察</h3><p>{data.observations?.length ?? 0} 次工具返回，所有来源均由后端注入。</p><button onClick={() => onFollowUp("继续检查当前证据尚未覆盖的退款原因、售后记录和批次质量")}>创建后续分析任务</button></aside></section>
  </>;
  return <>
    <section className="investigation-head"><div><span className="mode-badge"><Sparkles size={13} />{data.execution_mode}</span><h1>{data.anomaly?.summary ?? "商品经营异常调查"}</h1><p>{data.anomaly?.product_name} · {data.anomaly?.month} · {data.route_reason}</p></div><div className="investigation-actions"><button className="ghost-button" onClick={() => downloadFile("investigation.md", investigationMarkdown(data), "text/markdown")}><Download size={16} />导出报告</button><button className="primary-button small" onClick={() => { const q = window.prompt("请输入后续问题", "继续核对负向评论与退款异常的关系"); if (q?.trim()) onFollowUp(q.trim()); }}><Bot size={16} />继续追问</button></div></section>
    {data.execution_mode === "deterministic_local" && <div className="demo-notice"><Lightbulb size={16} /><span>本地确定性模式 · 外部模型已 deferred · 当前结果用于 Core MVP 本地回归，不代表真实 Planner 验收。</span></div>}
    <div className="demo-notice"><Lightbulb size={16} /><span>{data.demo_notice}</span></div>
    <section className="investigation-grid">
      <article className="panel timeline-panel"><PanelHead icon={Activity} title="调查轨迹" subtitle="仅展示可审计的动作与结果摘要" /><div className="timeline">{data.steps?.map((step: any, index: number) => <motion.div className={`timeline-step ${step.state}`} key={`${step.stage}-${index}`} initial={{ opacity: 0, x: -9 }} animate={{ opacity: 1, x: 0 }} transition={{ delay: index * .11 }}><span>{step.state === "stop" ? <ShieldCheck size={15} /> : index + 1}</span><div><small>{step.stage.toUpperCase()}</small><strong>{step.title}</strong><p>{step.detail}</p></div></motion.div>)}</div></article>
      <div className="investigation-main">
        <article className="diagnosis-card"><div className="diagnosis-icon"><ShieldCheck size={23} /></div><div><span>最终诊断 · {data.diagnosis?.confidence}</span><h2>{data.diagnosis?.title}</h2><p>{data.diagnosis?.summary}</p><div className="stop-line"><b>停止原因</b><code>{data.diagnosis?.stop_reason}</code></div></div></article>
        <article className="panel"><PanelHead icon={FileSearch} title="证据 Evidence" subtitle="原始评论证据可回查" action={<span className="verified"><ShieldCheck size={14} />证据校验通过</span>} /><div className="evidence-list">{data.evidence?.map((review: Review) => <EvidenceCard key={review.review_id} review={review} onClick={() => onEvidence(review)} />)}</div></article>
      </div>
      <aside className="missing-panel"><span className="eyebrow"><Database size={12} />待补充信号</span><h3>仍需补充的经营证据</h3><p>评论只能提供用户侧相关信号。要解释未覆盖部分，建议继续检查：</p>{data.diagnosis?.missing_signals?.map((signal: string, index: number) => <div key={signal}><span>{index + 1}</span><strong>{signal}</strong><ArrowRight size={14} /></div>)}<button onClick={() => onFollowUp(`继续分析评论之外的数据源：${(data.diagnosis?.missing_signals ?? []).join("、") || "退款原因、售后和批次质量"}`)}>创建后续分析任务</button></aside>
    </section>
  </>;
}

function ReviewsPage({ topics, signalCompare, reviews, reviewMeta, query, setQuery, onSearch, onReview }: { topics: Topic[]; signalCompare: ReviewSignalCompare | null; reviews: Review[]; reviewMeta: ReviewSearchMeta; query: string; setQuery: (value: string) => void; onSearch: (event: FormEvent) => void; onReview: (review: Review) => void }) {
  const [polarity, setPolarity] = useState<"all" | "positive" | "negative">("all");
  const visibleReviews = reviews.filter((review) => polarity === "all" || (polarity === "negative" ? review.rating <= 2 : review.rating >= 4));
  return <>
    <SimpleLead eyebrow="REVIEW INTELLIGENCE" title="评论洞察" description="同时查看正向、负向与混合反馈，让每个结论都能回到原文。" />
    <form className="review-search" onSubmit={onSearch}><Search size={19} /><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索问题、体验或评论原文，例如 stopped working" /><button>搜索证据</button></form>
    <section className="review-grid"><article className="panel"><PanelHead icon={BarChart3} title="主题与极性" subtitle="业务 Prediction Claim 信号" />{topics.length ? <TopicChart topics={topics} /> : <Empty text="暂无业务 Prediction 信号或样本不足" />}{signalCompare?.items.length ? <div className="signal-compare"><div className="compare-head"><b>主题 / 极性</b><b>{signalCompare.baseline_period}</b><b>{signalCompare.current_period}</b><b>变化</b></div>{signalCompare.items.slice(0,8).map((item) => <div className="compare-row" key={`${item.secondary_topic}-${item.polarity}`}><span>{item.secondary_topic}<small>{item.polarity}</small></span><span>{(item.baseline_share * 100).toFixed(1)}%</span><span>{(item.current_share * 100).toFixed(1)}%</span><strong className={item.share_delta > 0 ? "up" : item.share_delta < 0 ? "down" : ""}>{item.share_delta > 0 ? "+" : ""}{(item.share_delta * 100).toFixed(1)}pp</strong></div>)}</div> : <div className="topic-meta"><span>Baseline/Current 暂无足够 Prediction 样本</span></div>}</article><article className="panel review-summary"><PanelHead icon={ShieldCheck} title="证据健康度" subtitle="当前检索与数据边界" /><div className="health-ring"><div><strong>可回查</strong><span>原始评论</span></div></div><ul><li><span>Review ID 完整性</span><b>已启用</b></li><li><span>当前检索后端</span><b>{reviewMeta.retrieval_backend}</b></li><li><span>检索版本</span><b>{reviewMeta.retrieval_version}</b></li><li><span>Embedding</span><b>{reviewMeta.embedding_version ?? "无（Keyword）"}</b></li><li><span>Qdrant 向量后端</span><b>实验性</b></li><li><span>Topic 聚合信号</span><b>{topics.length ? "已接入" : "样本不足"}</b></li></ul></article></section>
    <article className="panel evidence-browser"><PanelHead icon={FileSearch} title="代表性评论" subtitle={`当前显示 ${visibleReviews.length} 条证据`} action={<div className="segmented"><button className={polarity === "all" ? "active" : ""} onClick={() => setPolarity("all")}>全部</button><button className={polarity === "positive" ? "active" : ""} onClick={() => setPolarity("positive")}>正向</button><button className={polarity === "negative" ? "active" : ""} onClick={() => setPolarity("negative")}>负向</button></div>} /><div className="evidence-grid">{visibleReviews.map((review) => <EvidenceCard key={review.review_id} review={review} onClick={() => onReview(review)} />)}</div>{!visibleReviews.length && <Empty text="没有找到匹配评论，请换一个关键词" />}</article>
  </>;
}

function SourcesPage() {
  const [kind, setKind] = useState("business_metrics"); const [file, setFile] = useState<File | null>(null); const [path, setPath] = useState(""); const [message, setMessage] = useState(""); const [sources, setSources] = useState<any[]>([]);
  useEffect(() => { api<{ items: any[] }>("/api/v1/data-sources").then((r) => setSources(r.items)).catch(() => undefined); }, []);
  async function upload() { if (!file) return; try { const result = await apiUpload<any>("/api/v1/data-sources/upload", file, { kind }); setSources((items) => [result, ...items.filter((x) => x.source_id !== result.source_id)]); setMessage(`已导入 ${result.record_count} 条，状态：${result.status}`); } catch (e) { setMessage(e instanceof Error ? e.message : "导入失败"); } }
  async function inspect() { if (!path) return; try { const result = await api<any>(`/api/v1/data-sources/sqlite/inspect?path=${encodeURIComponent(path)}`); setMessage(`连接成功：发现 ${result.tables.length} 张表`); } catch (e) { setMessage(e instanceof Error ? e.message : "连接失败"); } }
  return <><SimpleLead eyebrow="DATA SOURCES" title="数据源管理" description="上传业务 CSV，或探测本地 SQLite 数据库。没有真实数据源时，系统会保持安全的未配置状态。" /><section className="source-grid"><article className="panel source-card"><PanelHead icon={Download} title="上传业务文件" subtitle="导入后写入本地 Data Source Store" /><label className="source-label">数据类型<select value={kind} onChange={(e) => setKind(e.target.value)}><option value="business_metrics">经营指标</option><option value="refund_reasons">退款原因</option><option value="after_sales">售后记录</option><option value="inventory">库存</option></select></label><label className="file-drop"><input type="file" accept=".csv" onChange={(e) => setFile(e.target.files?.[0] ?? null)} /><strong>{file ? file.name : "选择 CSV 文件"}</strong><span>系统会校验字段、月份和数值范围</span></label><button className="primary-button" disabled={!file} onClick={upload}>导入并校验</button></article><article className="panel source-card"><PanelHead icon={Database} title="连接本地 SQLite" subtitle="只读探测表结构，不上传文件" /><label className="source-label">数据库路径<input value={path} onChange={(e) => setPath(e.target.value)} placeholder="/absolute/path/business.sqlite3" /></label><button className="ghost-button" disabled={!path} onClick={inspect}>测试连接</button><div className="source-hint">连接成功后可根据表结构配置导入映射；当前不会自动覆盖现有经营指标。</div></article></section>{message && <div className="demo-notice"><ShieldCheck size={16} />{message}</div>}<article className="panel source-list"><PanelHead icon={ClipboardCheck} title="已配置数据源" subtitle={`${sources.length} 个本地来源`} />{sources.length ? sources.map((source) => <div className="source-row" key={source.source_id}><span><strong>{source.name}</strong><small>{source.kind} · {source.source_id}</small></span><b>{source.record_count} 条 · {source.status}</b></div>) : <Empty text="暂无业务数据源，系统将继续使用安全的本地演示模式" />}</article></>;
}

function downloadFile(name: string, content: string, type: string) { const blob = new Blob([content], { type }); const url = URL.createObjectURL(blob); const link = document.createElement("a"); link.href = url; link.download = name; link.click(); URL.revokeObjectURL(url); }
function investigationMarkdown(data: any) { return [`# 商品经营异常诊断`, ``, `- 调查 ID：${data.request_id ?? "本地演示"}`, `- 商品：${data.product_id ?? data.anomaly?.product_id ?? "未知"}`, `- 时间：${data.period ?? data.anomaly?.month ?? "未知"}`, `- 状态：${data.status ?? data.diagnosis?.stop_reason ?? "未知"}`, `- 执行模式：${data.execution_mode ?? "未知"}`, ``, `## 结论`, ``, data.final_answer ?? data.diagnosis?.summary ?? "当前没有形成结论。", ``, `## 证据`, ``, ...(data.evidence ?? []).map((e: any, i: number) => `${i + 1}. ${e.quote ?? e.text ?? JSON.stringify(e)}`), ``, `## 边界`, ``, `评论信号仅表示相关性，不能单独证明经营异常的因果关系。`].join("\n"); }

function PanelHead({ icon: Icon, title, subtitle, action }: { icon: any; title: string; subtitle: string; action?: any }) {
  return <header className="panel-head"><div className="panel-icon"><Icon size={17} /></div><div><strong>{title}</strong><small>{subtitle}</small></div>{action && <div className="panel-action">{action}</div>}</header>;
}

function SimpleLead({ eyebrow, title, description }: { eyebrow: string; title: string; description: string }) {
  return <section className="simple-lead"><div><span>{eyebrow}</span><h1>{title}</h1><p>{description}</p></div><div className="lead-orbit"><i /><b /><em /></div></section>;
}

function AnomalyRow({ item, onClick }: { item: Anomaly; onClick: () => void }) {
  return <button className="anomaly-row" onClick={onClick}><span className={`severity-icon ${item.severity}`}><AlertTriangle size={16} /></span><span className="anomaly-copy"><strong>{item.summary}</strong><small>{item.month} · {item.signals.length} 个指标信号</small></span><Severity value={item.severity} /><ArrowRight size={15} /></button>;
}

function Severity({ value }: { value: string }) {
  const label = value === "high" ? "高" : value === "medium" ? "中" : "低";
  return <span className={`severity ${value}`}><i />{label}风险</span>;
}

function EvidenceCard({ review, onClick }: { review: Review; onClick: () => void }) {
  const negative = review.rating <= 2;
  return <button className="evidence-card" onClick={onClick}><div className="evidence-top"><span className={`rating ${negative ? "negative" : "positive"}`}><Star size={12} fill="currentColor" />{review.rating}</span><small>{review.created_at?.slice(0, 10) || "日期未知"}</small><ExternalLink size={14} /></div><strong>{review.title}</strong><p>{review.text}</p><div className="evidence-foot"><code>{review.review_id?.slice(0, 10)}…</code><span>查看原文</span></div></button>;
}

function ReviewDrawer({ review, onClose }: { review: Review; onClose: () => void }) {
  const [detail, setDetail] = useState<Review | null>(null);
  const [detailError, setDetailError] = useState("");
  useEffect(() => { let active = true; setDetail(null); setDetailError(""); api<{ data?: { review?: Review } }>(`/api/v1/reviews/${review.review_id}`).then((result) => active && setDetail(result.data?.review ?? null)).catch(() => active && setDetailError("原文回查失败")); return () => { active = false; }; }, [review.review_id]);
  const source = detail ?? review;
  return <><motion.div className="drawer-backdrop" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} onClick={onClose} /><motion.aside className="review-drawer" initial={{ x: "100%" }} animate={{ x: 0 }} exit={{ x: "100%" }} transition={{ type: "spring", stiffness: 340, damping: 34 }}><header><div><span>EVIDENCE DETAIL</span><strong>原始评论证据</strong></div><button onClick={onClose}><X size={19} /></button></header><div className="drawer-body"><div className="drawer-meta"><span><Star size={14} fill="currentColor" />{source.rating} 星</span><span>{source.created_at?.slice(0, 10)}</span><span>{source.source}</span></div><h2>{source.title}</h2><p>{source.text}</p><div className="id-block"><span>Review ID</span><code>{source.review_id}</code></div>{detailError ? <div className="error-banner">{detailError}</div> : <div className="guard-result"><ShieldCheck size={20} /><div><strong>Evidence Guard 校验通过</strong><span>{detail ? "已从业务 Review 源回查原文。" : "正在回查业务 Review 原文…"}</span></div></div>}<button className="primary-button"><ExternalLink size={16} />加入当前调查</button></div></motion.aside></>;
}

function LoadingScreen({ text = "正在读取经营数据…" }: { text?: string }) {
  return <div className="loading-screen"><div className="loading-orb"><Gauge size={25} /><i /><b /></div><strong>{text}</strong><span>正在对齐经营指标与评论证据</span></div>;
}

function Empty({ text }: { text: string }) {
  return <div className="empty"><PackageSearch size={24} /><span>{text}</span></div>;
}
