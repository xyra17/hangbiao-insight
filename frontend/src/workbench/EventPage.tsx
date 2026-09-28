import { useState } from "react";
import EventCommands from "./EventCommands";
import ParentSummary from "./ParentSummary";
import RuleControls from "./RuleControls";
import SnapshotDetails from "./SnapshotDetails";
import ScanControls from "./ScanControls";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Empty,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Job, type Page, type Product, type Shop } from "./api";
import type { components } from "./schema";

type Event = components["schemas"]["Event"];
type Snapshot = components["schemas"]["Snapshot"];
type Evidence = components["schemas"]["Evidence"];
type Observation = components["schemas"]["Observation"];
type Window = {
  start: string;
  end: string;
  days: number;
  ordered_units: number | null;
  order_count: number | null;
  sessions: number | null;
  unit_session_percent: string | null;
  raw_row_ids: string[];
};
type Daily = {
  status: string;
  reason?: string;
  cutoff?: string;
  baseline?: Window;
  current?: Window;
  limitations?: string[];
};
type CohortWindow = {
  start: string;
  end: string;
  numerator: number;
  denominator: number;
  rate: string | null;
  wilson_95?: [number, number] | null;
  unverified_first_shipment_orders?: number;
  order_evidence?: { first_shipped_at: string; affected: boolean; raw_row_ids: string[] }[];
};
type CohortMetric = {
  status: string;
  reason?: string;
  baseline?: CohortWindow;
  current?: CohortWindow;
  late_order_count?: number;
  unmatched_records?: number;
  observing_orders?: number;
  difference_interval_95?: [number, number] | null;
  refund_chains?: { currency: string; net_amount: string; classification: string; raw_row_ids: string[] }[];
};
const status: Record<string, string> = {
  ready: "范围完整",
  partial: "部分数据",
  missing: "缺少数据",
  invalid: "口径不一致",
  stale: "数据过期",
  pending: "待核查",
  processing: "处理中",
  observing: "待观察",
  closed: "已关闭",
};
const priorities = ["紧急", "高", "普通", "观察"];
const display = (value: number | string | null | undefined) => value ?? "缺失";

export default function EventPage({
  shop,
  canEdit,
}: {
  shop: Shop;
  canEdit: boolean;
}) {
  const cache = useQueryClient();
  const [page, setPage] = useState(1);
  const [object, setObject] = useState<string>();
  const [jobId, setJobId] = useState<string>();
  const [historicalDate, setHistoricalDate] = useState("");
  const [historyStart, setHistoryStart] = useState("");
  const [historyEnd, setHistoryEnd] = useState("");
  const [eventId, setEventId] = useState<string>();
  const [snapshotId, setSnapshotId] = useState<string>();
  const [traceId, setTraceId] = useState<string>();
  const trace = useQuery({
    queryKey: ["fact-row", traceId],
    queryFn: () =>
      request<components["schemas"]["Row"]>(`fact-rows/${traceId}/`),
    enabled: !!traceId,
  });
  const [eventStatus, setEventStatus] = useState<string>();
  const products = useQuery({
    queryKey: ["scan-objects", shop.id],
    queryFn: async () => {
      const results: Product[] = [];
      for (let page = 1; ; page++) {
        const data = await request<Page<Product>>(
          `products/?shop=${shop.id}&page=${page}`,
        );
        results.push(...data.results);
        if (!data.next) return results;
      }
    },
  });
  const options =
    products.data?.flatMap((p) => [
      {
        value: `product:${p.id}`,
        label: `${p.child_asin} / ${p.seller_sku} / ${p.fulfillment}`,
      },
    ]) ?? [];
  for (const asin of new Set(products.data?.map((p) => p.child_asin)))
    options.unshift({
      value: `asin:${asin}`,
      label: `${asin} / ASIN 原生汇总`,
    });
  const selected = object ?? options[0]?.value;
  const observations = useQuery({
    queryKey: ["observations", shop.id],
    queryFn: () =>
      request<Page<Observation>>(`observations/?shop=${shop.id}&status=open`),
  });
  const events = useQuery({
    queryKey: ["events", shop.id, page, eventStatus],
    queryFn: () =>
      request<Page<Event>>(
        `events/?shop=${shop.id}&page=${page}${eventStatus ? `&status=${eventStatus}` : ""}`,
      ),
    refetchInterval: 5000,
  });
  const scan = useMutation({
    mutationFn: () => {
      const product = products.data?.find(
        (p) => `product:${p.id}` === selected,
      );
      return request<Job>("snapshots/scan/", "POST", {
        shop: shop.id,
        child_asin: product?.child_asin ?? selected?.slice(5),
        product: product?.id,
        idempotency_key: crypto.randomUUID(),
      });
    },
    onSuccess: (j) => setJobId(j.id),
  });
  const recompute = useMutation({
    mutationFn: () => {
      const product = products.data?.find(
        (p) => `product:${p.id}` === selected,
      );
      return request<Job>("snapshots/recompute/", "POST", {
        shop: shop.id,
        child_asin: product?.child_asin ?? selected?.slice(5),
        product: product?.id,
        as_of: historicalDate,
        idempotency_key: crypto.randomUUID(),
      });
    },
    onSuccess: (j) => {
      setSnapshotId(undefined);
      setJobId(j.id);
    },
  });
  const recomputeRange = useMutation({
    mutationFn: () => {
      const product = products.data?.find((p) => `product:${p.id}` === selected);
      return request<Job>("snapshots/recompute-range/", "POST", {
        shop: shop.id, child_asin: product?.child_asin ?? selected?.slice(5),
        product: product?.id, from_date: historyStart, to_date: historyEnd,
        idempotency_key: crypto.randomUUID(),
      });
    },
    onSuccess: (j) => { setSnapshotId(undefined); setJobId(j.id); },
  });
  const job = useQuery({
    queryKey: ["scan-job", jobId],
    queryFn: () => request<Job>(`jobs/${jobId}/`),
    enabled: !!jobId,
    refetchInterval: (q) =>
      ["completed", "failed", "cancelled"].includes(q.state.data?.status ?? "")
        ? false
        : 1000,
  });
  const completedId = job.data?.result.snapshot_id;
  const activeSnapshot =
    snapshotId ?? (typeof completedId === "string" ? completedId : undefined);
  const snapshot = useQuery({
    queryKey: ["metric-snapshot", activeSnapshot],
    queryFn: () => request<Snapshot>(`snapshots/${activeSnapshot}/`),
    enabled: !!activeSnapshot,
  });
  const evidence = useQuery({
    queryKey: ["event-evidence", eventId],
    queryFn: () => request<Page<Evidence>>(`events/${eventId}/evidence/`),
    enabled: !!eventId,
  });
  const daily = snapshot.data?.payload.daily as Daily | undefined;
  const cohort = snapshot.data?.payload.cohort as
    | { reason?: string; refunds?: CohortMetric; returns?: CohortMetric }
    | undefined;
  const error =
    products.error ||
    events.error ||
    scan.error ||
    recompute.error ||
    recomputeRange.error ||
    job.error ||
    snapshot.error ||
    evidence.error;
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>事件队列</Typography.Title>
          <p>先核对数据范围，再看变化；指标异常是调查起点。</p>
        </div>
        <Space>
          <Button
            href={`/api/v1/events/export/?shop=${shop.id}${eventStatus ? `&status=${encodeURIComponent(eventStatus)}` : ""}`}
          >
            导出当前筛选 CSV
          </Button>
          <Button
            onClick={() =>
              void cache.invalidateQueries({ queryKey: ["events"] })
            }
          >
            刷新队列
          </Button>
        </Space>
      </div>
      <ScanControls shop={shop} canEdit={canEdit} onEvent={setEventId} />
      {error && <Alert type="error" title={error.message} />}
      <Space wrap style={{ margin: "22px 0" }}>
        <Select
          aria-label="扫描分析对象"
          style={{ minWidth: 320 }}
          value={selected}
          options={options}
          onChange={(value) => {
            setObject(value);
            setJobId(undefined);
            setSnapshotId(undefined);
          }}
        />
        <Button
          type="primary"
          disabled={
            !selected ||
            ["queued", "running", "retry_wait"].includes(job.data?.status ?? "")
          }
          loading={scan.isPending}
          onClick={() => {
            setSnapshotId(undefined);
            scan.mutate();
          }}
        >
          扫描已发布数据
        </Button>
        {job.data && (
          <Tag>
            {job.data.status === "completed"
              ? "扫描完成"
              : job.data.status === "failed"
                ? "扫描失败"
                : "扫描处理中"}
          </Tag>
        )}
        {job.data?.error && (
          <Typography.Text type="danger">{job.data.error}</Typography.Text>
        )}
        {activeSnapshot && (
          <Button onClick={() => setSnapshotId(activeSnapshot)}>
            查看计算依据
          </Button>
        )}
        {canEdit && (
          <RuleControls
            key={selected}
            shopId={shop.id}
            asin={
              selected?.startsWith("asin:")
                ? selected.slice(5)
                : products.data?.find((p) => `product:${p.id}` === selected)
                    ?.child_asin
            }
            productId={
              selected?.startsWith("product:") ? selected.slice(8) : undefined
            }
          />
        )}
        <ParentSummary
          shop={shop}
          products={products.data ?? []}
          onChild={(asin) => {
            setObject(`asin:${asin}`);
            setJobId(undefined);
            setEventStatus(undefined);
            setPage(1);
          }}
        />
        <Select
          aria-label="事件状态"
          allowClear
          placeholder="全部状态"
          value={eventStatus}
          style={{ width: 150 }}
          onChange={(v) => {
            setEventStatus(v);
            setPage(1);
          }}
          options={["pending", "processing", "observing", "closed"].map(
            (v) => ({ value: v, label: status[v] }),
          )}
        />
      </Space>
      {canEdit && (
        <Space wrap style={{ marginBottom: 20 }}>
          <Input
            aria-label="历史重算日期"
            type="date"
            value={historicalDate}
            disabled={recompute.isPending}
            onChange={(event) => setHistoricalDate(event.target.value)}
          />
          <Button
            disabled={
              !historicalDate ||
              !selected ||
              ["queued", "running", "retry_wait"].includes(
                job.data?.status ?? "",
              )
            }
            loading={recompute.isPending}
            onClick={() => recompute.mutate()}
          >
            重算历史日期
          </Button>
          <Typography.Text type="secondary">
            最近 90
            天；用当前已发布数据和规则重算所选日期之前的完整日，不修改当前事件或发送通知。
          </Typography.Text>
        </Space>
      )}
      {canEdit && (
        <section>
          <Space wrap>
            <Input aria-label="历史回补开始日期" type="date" value={historyStart} onChange={(event) => setHistoryStart(event.target.value)} />
            <Input aria-label="历史回补结束日期" type="date" value={historyEnd} onChange={(event) => setHistoryEnd(event.target.value)} />
            <Button loading={recomputeRange.isPending} disabled={!selected || !historyStart || !historyEnd || historyStart > historyEnd || ["queued", "running", "retry_wait"].includes(job.data?.status ?? "")} onClick={() => recomputeRange.mutate()}>回补历史区间</Button>
          </Space>
          <p className="wb-secondary">最多回看 730 天，单次不超过 366 天。按当前已发布数据回算，保留旧快照，不改变当前事件。</p>
          {job.data?.kind === "recompute_metrics_range" && job.data.status === "completed" && (
            <>
              <Alert type="success" title="历史区间回补完成" description={`实际完成时间：${job.data.finished_at ?? "未知"}`} />
              <Select aria-label="查看回补日期" placeholder="选择日期查看计算依据" style={{ width: 260 }}
                onChange={(value) => setSnapshotId(value)}
                options={((job.data.result.snapshot_ids ?? []) as string[]).map((id, index) => {
                  const first = new Date(`${job.data!.result.from_date}T00:00:00Z`);
                  first.setUTCDate(first.getUTCDate() + index);
                  return { value: id, label: first.toISOString().slice(0, 10) };
                })}
              />
            </>
          )}
        </section>
      )}
      <Typography.Title level={4} style={{ marginTop: 24 }}>
        观察线索
      </Typography.Title>
      <Alert
        type="info"
        title="样本不足只进入观察线索，不创建异常事件，也不发送通知。"
      />
      <Table<Observation>
        rowKey="id"
        size="small"
        dataSource={observations.data?.results}
        loading={observations.isLoading}
        pagination={false}
        columns={[
          { title: "子 ASIN", dataIndex: "child_asin" },
          { title: "线索", dataIndex: "title" },
          { title: "指标", dataIndex: "metric" },
          { title: "最近核查", dataIndex: "last_seen" },
          { title: "计算依据", render: (_, row) => <Button onClick={() => setSnapshotId(row.snapshot)}>查看线索快照</Button> },
        ]}
        locale={{ emptyText: "暂无样本不足观察线索" }}
      />
      <Table<Event>
        rowKey="id"
        dataSource={events.data?.results}
        loading={events.isLoading}
        scroll={{ x: 850 }}
        pagination={{
          current: page,
          pageSize: 50,
          total: events.data?.count,
          onChange: setPage,
          showSizeChanger: false,
        }}
        columns={[
          {
            title: "紧急度",
            render: (_, e) => (
              <Tag
                color={
                  e.priority === 0
                    ? "red"
                    : e.priority === 1
                      ? "orange"
                      : "blue"
                }
              >
                {priorities[e.priority]}
              </Tag>
            ),
          },
          { title: "子 ASIN", dataIndex: "child_asin" },
          { title: "待核查事项", dataIndex: "title" },
          { title: "问题组", render: (_, row) => row.group_title || "未分组" },
          {
            title: "状态",
            render: (_, e) => (
              <Space>
                {status[e.status] ?? e.status}
                {e.needs_data && <Tag color="gold">待补数据</Tag>}
              </Space>
            ),
          },
          { title: "数据截至", dataIndex: "last_cutoff" },
          {
            title: "持续性",
            render: (_, e) =>
              e.consecutive_days >= 2
                ? "新增完整日仍满足条件"
                : "首次或尚未确认持续",
          },
          {
            title: "依据",
            render: (_, e) => (
              <Button type="link" onClick={() => setEventId(e.id)}>
                查看证据
              </Button>
            ),
          },
        ]}
        locale={{
          emptyText: (
            <Empty description="尚无事件。可扫描已发布的主来源报表；缺少数据不会伪造异常。" />
          ),
        }}
      />
      <Drawer
        title="事件证据"
        size="large"
        open={!!eventId}
        onClose={() => setEventId(undefined)}
      >
        {eventId && <EventCommands key={eventId} eventId={eventId} />}{" "}
        <Table<Evidence>
          rowKey="id"
          dataSource={evidence.data?.results}
          pagination={false}
          columns={[
            { title: "指标", dataIndex: "metric" },
            { title: "发现时间", dataIndex: "created_at" },
            {
              title: "计算依据",
              render: (_, e) => (
                <Button onClick={() => setSnapshotId(e.snapshot)}>
                  查看快照
                </Button>
              ),
            },
          ]}
        />
        {evidence.data && evidence.data.count > 50 && (
          <Typography.Text>当前显示最近 50 条证据。</Typography.Text>
        )}
      </Drawer>
      <Drawer
        title="计算依据与数据范围"
        size="large"
        open={!!snapshotId}
        onClose={() => setSnapshotId(undefined)}
      >
        {snapshot.data && (
          <>
            <Descriptions
              column={1}
              items={[
                {
                  key: "object",
                  label: "对象",
                  children: `${snapshot.data.child_asin} / ${snapshot.data.product ? "SKU 与履约对象" : "ASIN 原生汇总"}`,
                },
                { key: "zone", label: "站点时区", children: shop.timezone },
                {
                  key: "mode",
                  label: "计算用途",
                  children:
                    snapshot.data.payload.mode === "historical"
                      ? `历史重算 · 分析日期 ${snapshot.data.local_date} · 使用重算时已发布数据与配置`
                      : "当前扫描",
                },
                {
                  key: "version",
                  label: "计算规则",
                  children: snapshot.data.rule_version,
                },
                {
                  key: "date",
                  label: "数据截至",
                  children: daily?.cutoff ?? "暂无完整日",
                },
              ]}
            />
            <Typography.Title level={4}>销售与流量</Typography.Title>
            <Alert
              type={daily?.status === "ready" ? "info" : "warning"}
              title={status[daily?.status ?? "missing"]}
              description={daily?.reason}
            />
            <Table
              rowKey="name"
              pagination={false}
              dataSource={[
                { name: "基线", ...daily?.baseline },
                { name: "当前", ...daily?.current },
              ]}
              columns={[
                {
                  title: "窗口",
                  render: (_, w) => (
                    <>
                      {w.name}
                      <div className="wb-secondary">
                        {w.start} 至 {w.end}
                      </div>
                    </>
                  ),
                },
                {
                  title: "订购件数",
                  render: (_, w) => display(w.ordered_units),
                },
                { title: "订单数", render: (_, w) => display(w.order_count) },
                { title: "Sessions", render: (_, w) => display(w.sessions) },
                {
                  title: "件数／会话",
                  render: (_, w) =>
                    w.unit_session_percent != null
                      ? `${Number(w.unit_session_percent).toFixed(2)}%`
                      : "不适用或缺失",
                },
              ]}
            />
            <p className="wb-secondary">
              两个窗口长度不同，数量按日均比较；件数／会话可超过
              100%，不是购买用户概率。
            </p>
            <Typography.Title level={5}>当前窗口原始行</Typography.Title>
            <Space wrap>
              {daily?.current?.raw_row_ids.map((id, i) => (
                <Button key={id} onClick={() => setTraceId(id)}>
                  第 {i + 1} 条依据
                </Button>
              ))}
            </Space>
            <Typography.Title level={4}>成熟订单队列</Typography.Title>
            {cohort?.reason && <Alert type="warning" title={cohort.reason} />}
            {(["refunds", "returns"] as const).map(
              (kind) =>
                cohort?.[kind] && (
                  <section key={kind}>
                    <Typography.Title level={5}>
                      {kind === "refunds" ? "退款订单率" : "退货订单率"}
                    </Typography.Title>
                    <p>
                      {status[cohort[kind]!.status]} · {cohort[kind]!.reason}
                    </p>
                    {[cohort[kind]!.baseline, cohort[kind]!.current].map(
                      (w, i) =>
                        w && (
                          <p key={i}>
                            {i === 0 ? "基线" : "当前"}（{w.start} 至 {w.end}
                            ）：{w.numerator} / {w.denominator} 个成熟订单，
                            {w.rate == null
                              ? "不适用"
                              : `${(Number(w.rate) * 100).toFixed(2)}%`}
                            {w.wilson_95 && (
                              <span>
                                ；95% 区间 {(w.wilson_95[0] * 100).toFixed(2)}%
                                至 {(w.wilson_95[1] * 100).toFixed(2)}%
                              </span>
                            )}
                          </p>
                        ),
                    )}
                    {[cohort[kind]!.baseline, cohort[kind]!.current].map((window, index) => window?.order_evidence && (
                      <section key={`orders-${index}`}>
                        <Typography.Title level={5}>{index === 0 ? "基线" : "当前"}分母订单与首次发货依据</Typography.Title>
                        <Table
                          rowKey={(row) => row.raw_row_ids.join(":")}
                          size="small"
                          pagination={{ pageSize: 10 }}
                          dataSource={window.order_evidence}
                          columns={[
                            { title: "首次发货时间", dataIndex: "first_shipped_at" },
                            { title: "计入分子", render: (_, row) => row.affected ? "是" : "否" },
                            { title: "发货原始行", render: (_, row) => <Space wrap>{row.raw_row_ids.map((id, n) => <Button key={id} onClick={() => setTraceId(id)}>依据 {n + 1}</Button>)}</Space> },
                          ]}
                        />
                      </section>
                    ))}
                    {kind === "refunds" && cohort[kind]!.refund_chains && (
                      <section>
                        <Typography.Title level={5}>退款交易链与冲正依据</Typography.Title>
                        <Table
                          rowKey={(row) => row.raw_row_ids.join(":")}
                          size="small"
                          pagination={{ pageSize: 10 }}
                          dataSource={cohort[kind]!.refund_chains}
                          columns={[
                            { title: "净额", render: (_, row) => `${row.currency} ${row.net_amount}` },
                            { title: "判定", render: (_, row) => ({ reversed: "净额不大于零，排除", within_observation: "观察期内有效退款", late: "观察期外退款", unmatched: "无法关联订单", before_shipment: "早于发货，待核查" }[row.classification] ?? row.classification) },
                            { title: "原始交易", render: (_, row) => <Space wrap>{row.raw_row_ids.map((id, n) => <Button key={id} onClick={() => setTraceId(id)}>交易 {n + 1}</Button>)}</Space> },
                          ]}
                        />
                      </section>
                    )}
                    {cohort[kind]!.difference_interval_95 && (
                      <p>
                        当前与基线差值的 95% 区间：
                        {cohort[kind]!.difference_interval_95!.map((v) =>
                          (v * 100).toFixed(2),
                        ).join(" 至 ")}{" "}
                        个百分点。
                      </p>
                    )}
                    <p>
                      尚在观察期：{cohort[kind]!.observing_orders ?? "未知"}{" "}
                      个订单。
                    </p>
                    <p className="wb-secondary">
                      观察期外发生：{cohort[kind]!.late_order_count ?? "未知"}{" "}
                      个订单；无法关联：
                      {cohort[kind]!.unmatched_records ?? "未知"} 条。
                    </p>
                  </section>
                ),
            )}
            <SnapshotDetails
              payload={snapshot.data.payload}
              onTrace={setTraceId}
            />
          </>
        )}
      </Drawer>
      <Drawer
        title="原始行追溯"
        open={!!traceId}
        onClose={() => setTraceId(undefined)}
        size="large"
      >
        {trace.error && <Alert type="error" title={trace.error.message} />}
        {trace.data && (
          <>
            <p>原文件行号：{trace.data.line}</p>
            <Typography.Title level={5}>原始字段</Typography.Title>
            <pre className="wb-raw">
              {JSON.stringify(trace.data.raw, null, 2)}
            </pre>
            <Typography.Title level={5}>规范化字段</Typography.Title>
            <pre className="wb-raw">
              {JSON.stringify(trace.data.normalized, null, 2)}
            </pre>
          </>
        )}
      </Drawer>
    </>
  );
}
