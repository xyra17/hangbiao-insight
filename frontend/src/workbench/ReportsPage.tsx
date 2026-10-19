import RefreshHistory from "./RefreshHistory";
import EvidenceSearch from "./EvidenceSearch";
import useServerPage from "./useServerPage";
import ListError from "./ListError";
import { useEffect, useRef, useState } from "react";
import { isActiveJob, jobStatusLabel } from "./executionStatus";
import {
  Alert,
  Button,
  Card,
  Collapse,
  Descriptions,
  Empty,
  Form,
  Input,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Job, type Page, type Product, type Shop } from "./api";

type Report = {
  id: string;
  child_asin: string;
  status: string;
  report_version: number;
  payload: {
    summary?: string;
    signals?: unknown[];
    claims?: Claim[];
    limitations?: string[];
    gaps?: string[];
    status?: string;
    citations?: unknown[];
    evidence_guard?: { valid: boolean; errors?: string[] };
    diagnosis?: {
      scenarios?: Record<
        string,
        { label: string; status: string; gaps: string[] }
      >;
      candidates?: {
        title: string;
        status: string;
        support_refs: string[];
        missing_checks: string[];
        causal_status?: string;
        causal_claim_allowed?: boolean;
      }[];
      coverage?: {
        gate: string;
        windows?: unknown[];
        effective_denominator?: {
          start?: string;
          end?: string;
          analyzed?: number;
        }[];
        gate_reason?: string;
      };
      safety?: { urgent: boolean; route: string };
      experience_links?: {
        status: string;
        linked_order_count?: number;
        source_counts?: Record<string, number>;
        unlinked?: { source_type: string; record_id: string; reason: string }[];
        limitations?: string[];
      };
      evidence_facts?: {
        metric?: string;
        source?: string;
        statistics?: Record<string, unknown>;
      }[];
      limitations?: string[];
    };
  };
  created_at: string;
};
type Claim = {
  claim_id: string;
  review_id: string;
  topic: { primary: string; secondary: string };
  polarity: string;
  evidence_span: string;
  source_refs: { type: string; raw_row_id: string }[];
};

export default function ReportsPage(props: {
  shop: Shop;
  products: Product[];
}) {
  return <ReportsContent key={props.shop.id} {...props} />;
}

function ReportsContent({
  shop,
  products,
}: {
  shop: Shop;
  products: Product[];
}) {
  const cache = useQueryClient();
  const [selected, setSelected] = useState<Report | null>(null);
  const [job, setJob] = useState<Job | null>(null);
  const generation = useRef(0);
  const openRevision = useMutation({
    mutationFn: async (id: string) => {
      const openedGeneration = generation.current;
      return {
        report: await request<Report>(`reports/${id}/`),
        generation: openedGeneration,
      };
    },
    onSuccess: (value) => {
      if (value.generation === generation.current) setSelected(value.report);
    },
  });
  const reports = useServerPage<Report>({
    key: "reports",
    scope: shop.id,
    path: `reports/?shop=${shop.id}`,
  });
  const runs = useQuery({
    queryKey: ["semantic-runs", shop.id],
    queryFn: () =>
      request<
        Page<{
          id: string;
          child_asin: string;
          status: string;
          provider: string;
          accepted_claims: number;
          rejected_claims: number;
          error: string;
        }>
      >(`semantic-runs/?shop=${shop.id}`),
  });
  const start = useMutation({
    mutationFn: (data: {
      child_asin: string;
      product?: string;
      provider: string;
    }) =>
      request<Job>("semantic-runs/", "POST", {
        shop: shop.id,
        ...data,
        idempotency_key: crypto.randomUUID(),
      }),
    onMutate: () => {
      generation.current += 1;
      setJob(null);
      setSelected(null);
    },
    onSuccess: (value) => {
      setJob(value);
      void cache.invalidateQueries({ queryKey: ["semantic-runs", shop.id] });
    },
  });
  const selectedJob = useQuery({
    queryKey: ["semantic-job", shop.id, job?.id],
    queryFn: () => request<Job>(`jobs/${job?.id}/`),
    enabled: (query) =>
      !!job?.id && isActiveJob(query.state.data?.status ?? job.status),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) =>
      query.state.status !== "error" &&
      isActiveJob(query.state.data?.status ?? job?.status)
        ? 1200
        : false,
  });
  const activeJob = selectedJob.data ?? job;
  useEffect(() => {
    if (!activeJob || isActiveJob(activeJob.status)) return;
    void cache.invalidateQueries({ queryKey: ["semantic-runs", shop.id] });
    void cache.invalidateQueries({ queryKey: ["reports", shop.id] });
  }, [activeJob?.id, activeJob?.status, cache, shop.id]);
  const reportId =
    activeJob?.status === "completed" ? activeJob.result?.report_id : undefined;
  const report = useQuery({
    queryKey: ["report", shop.id, reportId],
    queryFn: () => request<Report>(`reports/${reportId}/`),
    enabled: typeof reportId === "string",
  });
  const options = products.map((p) => ({
    value: p.child_asin,
    label: `${p.child_asin} / ${p.seller_sku}`,
  }));
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>调查报告</Typography.Title>
          <p>
            报告只引用已发布指标快照和通过证据校验的评论，模型状态与确定性检查分开显示。
          </p>
        </div>
      </div>
      <Card title="运行语义取证" style={{ marginBottom: 20 }}>
        <Form
          name="semantic-run"
          className="wb-semantic-form"
          layout="vertical"
          onFinish={(data) => start.mutate(data)}
          initialValues={{ provider: "deterministic" }}
        >
          <Form.Item
            name="child_asin"
            label="子 ASIN"
            rules={[{ required: true }]}
          >
            <Select
              showSearch
              style={{ width: 260 }}
              options={options}
              placeholder="选择授权商品"
            />
          </Form.Item>
          <Form.Item name="provider" label="适配器">
            <Select
              style={{ width: 180 }}
              options={[
                { value: "deterministic", label: "确定性测试适配器" },
                { value: "deepseek", label: "DeepSeek（需配置密钥）" },
                { value: "lmstudio", label: "LM Studio（需配置）" },
                { value: "qwen", label: "Qwen（需配置）" },
              ]}
            />
          </Form.Item>
          <Button
            type="primary"
            aria-label="开始取证"
            aria-busy={start.isPending}
            htmlType="submit"
            loading={start.isPending}
          >
            开始取证
          </Button>
        </Form>
        {start.error && (
          <Alert
            type="error"
            showIcon
            title={start.error.message}
            style={{ marginTop: 12 }}
          />
        )}
        {activeJob && (
          <Alert
            type={activeJob.status === "failed" ? "error" : "info"}
            showIcon
            title={`后台任务：${jobStatusLabel(activeJob.status)}${activeJob.error ? `；${activeJob.error}` : ""}`}
            style={{ marginTop: 12 }}
          />
        )}
        {selectedJob.error && (
          <Alert
            type="error"
            showIcon
            title="任务状态读取失败，已暂停自动刷新"
            description={selectedJob.error.message}
            action={
              <Button onClick={() => void selectedJob.refetch()}>
                重新读取状态
              </Button>
            }
          />
        )}
        {report.error && (
          <ListError
            title="报告读取失败"
            error={report.error}
            retry={() => report.refetch()}
          />
        )}
        {report.data && (
          <Button
            type="link"
            onClick={() => {
              setSelected(report.data);
              void cache.invalidateQueries({ queryKey: ["reports", shop.id] });
            }}
          >
            查看刚生成的报告
          </Button>
        )}
      </Card>
      {runs.data?.results.some((run) => run.provider !== "deterministic") && (
        <Alert
          type="warning"
          showIcon
          title="真实模型结果必须在供应商、区域、预算和密钥确认后才能作为业务验收依据。"
          style={{ marginBottom: 16 }}
        />
      )}
      <div className="wb-section-heading">
        <Typography.Title level={4}>历史报告</Typography.Title>
        <Space wrap>
          <Typography.Text type="secondary">
            按店铺查看已保存版本
          </Typography.Text>
          <Button
            onClick={() => void reports.refetch()}
            loading={reports.isFetching}
          >
            刷新报告列表
          </Button>
        </Space>
      </div>
      <ListError
        title="报告列表读取失败"
        error={reports.error}
        retry={() => reports.refetch()}
      />
      <ListError
        title="取证记录读取失败"
        error={runs.error}
        retry={() => runs.refetch()}
      />
      <Table<Report>
        rowKey="id"
        dataSource={reports.data?.results ?? []}
        loading={reports.isLoading}
        locale={{
          emptyText: (
            <Empty
              description={
                reports.error ? "报告列表读取失败" : "还没有标准报告"
              }
            />
          ),
        }}
        scroll={{ x: 850 }}
        pagination={reports.pagination}
        columns={[
          { title: "子 ASIN", dataIndex: "child_asin" },
          {
            title: "状态",
            render: (_, r) => (
              <Tag color={r.status === "published" ? "default" : "gold"}>
                {r.status === "published" ? "已发布" : r.status}
              </Tag>
            ),
          },
          { title: "报告版本", dataIndex: "report_version" },
          {
            title: "证据",
            render: (_, r) =>
              `${r.payload.claims?.length ?? 0} 条评论 claim / ${r.payload.signals?.length ?? 0} 个指标信号`,
          },
          {
            title: "生成时间",
            render: (_, r) => new Date(r.created_at).toLocaleString("zh-CN"),
          },
          {
            title: "操作",
            render: (_, r) => (
              <Button type="link" onClick={() => setSelected(r)}>
                查看证据
              </Button>
            ),
          },
        ]}
      />
      <Collapse
        activeKey={selected ? ["report"] : []}
        onChange={(keys) => {
          if (!keys.length) setSelected(null);
        }}
        items={
          selected
            ? [
                {
                  key: "report",
                  label: `报告 ${selected.child_asin} · v${selected.report_version}`,
                  children: (
                    <>
                      <RefreshHistory
                        kind="reports"
                        id={selected.id}
                        onOpen={(id) => openRevision.mutate(id)}
                      />
                      {openRevision.error && (
                        <Alert
                          type="error"
                          title={openRevision.error.message}
                        />
                      )}
                      <ReportDetail report={selected} />
                    </>
                  ),
                },
              ]
            : []
        }
      />
      <EvidenceSearch shopId={shop.id} products={products} />
    </>
  );
}

function ReportDetail({ report }: { report: Report }) {
  const payload = report.payload;
  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      <Space>
        <Button href={`/api/v1/reports/${report.id}/export/?file_type=html`}>
          下载报告 HTML
        </Button>
        <Button href={`/api/v1/reports/${report.id}/export/?file_type=json`}>
          下载报告 JSON
        </Button>
      </Space>
      <Typography.Paragraph>{payload.summary}</Typography.Paragraph>
      {payload.status === "partial" && (
        <Alert type="warning" showIcon title="报告部分完成，以下缺口尚未解决" />
      )}
      {(payload.gaps ?? []).map((gap) => (
        <Alert key={gap} type="warning" title={gap} />
      ))}
      {payload.diagnosis && (
        <Card
          size="small"
          title="标准诊断与候选解释"
          style={{ marginBottom: 16 }}
        >
          <Space wrap>
            <Tag
              color={
                payload.diagnosis.coverage?.gate === "ready" ||
                payload.diagnosis.coverage?.gate === "pass"
                  ? "green"
                  : "orange"
              }
            >
              覆盖：{payload.diagnosis.coverage?.gate ?? "缺失"}
            </Tag>
            {payload.diagnosis.coverage?.effective_denominator && (
              <Tag>
                有效样本：
                {payload.diagnosis.coverage.effective_denominator
                  .map((window) => window.analyzed ?? 0)
                  .join(" / ")}
              </Tag>
            )}
            {payload.diagnosis.safety?.urgent && (
              <Tag color="red">安全反馈：已生成人工核查任务</Tag>
            )}
          </Space>
          {payload.diagnosis.coverage?.gate_reason && (
            <Typography.Paragraph type="secondary">
              覆盖门槛：{payload.diagnosis.coverage.gate_reason}
            </Typography.Paragraph>
          )}
          <Typography.Paragraph type="secondary">
            场景检查：
            {Object.values(payload.diagnosis.scenarios ?? {})
              .map((item) => `${item.label}（${item.status}）`)
              .join(" · ")}
          </Typography.Paragraph>
          {payload.evidence_guard && !payload.evidence_guard.valid && (
            <Alert
              type="error"
              showIcon
              title="证据校验未通过，候选解释已降级"
              description={payload.evidence_guard.errors?.join("；")}
            />
          )}
          {payload.diagnosis.experience_links && (
            <Card
              size="small"
              title="订单经历关联"
              style={{ marginBottom: 12 }}
            >
              <Typography.Paragraph>
                状态：{payload.diagnosis.experience_links.status}
                ；已关联订单经历：
                {payload.diagnosis.experience_links.linked_order_count ?? 0}
              </Typography.Paragraph>
              <Typography.Text type="secondary">
                未关联记录：
                {payload.diagnosis.experience_links.unlinked?.length ?? 0}{" "}
                条。未关联来源不会合并为独立受影响订单。
              </Typography.Text>
              {(payload.diagnosis.experience_links.limitations ?? []).map(
                (item) => (
                  <Typography.Paragraph key={item} type="secondary">
                    {item}
                  </Typography.Paragraph>
                ),
              )}
            </Card>
          )}
          {(payload.diagnosis.evidence_facts ?? []).some(
            (fact) => fact.statistics,
          ) && (
            <Card size="small" title="指标事实" style={{ marginBottom: 12 }}>
              {(payload.diagnosis.evidence_facts ?? [])
                .filter((fact) => fact.statistics)
                .slice(0, 12)
                .map((fact, index) => (
                  <Descriptions
                    key={`${fact.metric}-${index}`}
                    size="small"
                    column={2}
                    bordered
                    style={{ marginBottom: 8 }}
                  >
                    <Descriptions.Item label="指标">
                      {fact.metric ?? "未知"}
                    </Descriptions.Item>
                    <Descriptions.Item label="单位">
                      {String(fact.statistics?.unit ?? "未知")}
                    </Descriptions.Item>
                    <Descriptions.Item label="基线">
                      {String(fact.statistics?.baseline ?? "未知")}
                    </Descriptions.Item>
                    <Descriptions.Item label="当前">
                      {String(fact.statistics?.current ?? "未知")}
                    </Descriptions.Item>
                    <Descriptions.Item label="变化">
                      {String(fact.statistics?.delta ?? "未知")}
                    </Descriptions.Item>
                    <Descriptions.Item label="计算方法">
                      {String(fact.statistics?.method ?? "未知")}
                    </Descriptions.Item>
                  </Descriptions>
                ))}
            </Card>
          )}
          {(payload.diagnosis.candidates ?? []).map((candidate) => (
            <Card
              size="small"
              key={candidate.title}
              style={{ marginBottom: 8 }}
            >
              <Typography.Text strong>{candidate.title}</Typography.Text>
              <Typography.Paragraph type="secondary">
                状态：{candidate.status}；支持引用{" "}
                {candidate.support_refs.length} 条；待核查：
                {candidate.missing_checks.join("；")}
              </Typography.Paragraph>
              <Typography.Text type="secondary">
                因果状态：{candidate.causal_status ?? "not_established"}；
                {candidate.causal_claim_allowed === true
                  ? "允许直接归因"
                  : "仅作待核查线索，不能直接归因"}
              </Typography.Text>
            </Card>
          ))}
        </Card>
      )}
      <Typography.Title level={5}>
        评论证据（{payload.claims?.length ?? 0}）
      </Typography.Title>
      {(payload.claims ?? []).map((claim) => (
        <Card size="small" key={claim.claim_id}>
          <Space wrap>
            <Tag>{claim.topic.secondary}</Tag>
            <Tag color={claim.polarity === "negative" ? "red" : "green"}>
              {claim.polarity}
            </Tag>
            <span>评论 {claim.review_id}</span>
          </Space>
          <Typography.Paragraph style={{ margin: "8px 0 0" }}>
            “{claim.evidence_span}”
          </Typography.Paragraph>
          <Typography.Text type="secondary">
            来源行：{claim.source_refs.map((ref) => ref.raw_row_id).join(", ")}
          </Typography.Text>
        </Card>
      ))}
      <Typography.Title level={5}>限制与缺口</Typography.Title>
      <ul>
        {(payload.limitations ?? []).map((item) => (
          <li key={item}>{item}</li>
        ))}
      </ul>
    </Space>
  );
}
