import RefreshHistory from "./RefreshHistory";
import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Collapse,
  Empty,
  Form,
  Input,
  InputNumber,
  Select,
  Space,
  Spin,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request, type Job, type Product, type Shop } from "./api";

type Run = {
  id: string;
  query: string;
  child_asin: string;
  status: string;
  steps_used: number;
  max_steps: number;
  budget_units: number;
  stop_reason: string;
  candidates?: { child_asin: string; product_id: string; seller_sku: string }[];
  decisions?: {
    phase?: string;
    next_tool?: string;
    hypothesis?: string;
    counter_evidence_query?: string;
    stop_reason?: string;
    planner?: string;
  }[];
  counter_evidence?: unknown[];
  budget?: {
    remaining_units: number;
    model_attempts: number;
    model_tokens: number;
    model_cost: string;
    elapsed_ms: number;
    duplicate_queries: number;
  };
  tool_calls?: {
    sequence: number;
    tool_name: string;
    authorization: string;
    grant_version?: number | null;
    status: string;
    output_refs: string[];
    scope: Record<string, unknown>;
  }[];
  tool_permissions?: {
    tool_name: string;
    mode: string;
    roles: string[];
    description: string;
    external_write: boolean;
  }[];
  result: {
    hypotheses?: unknown[];
    evidence?: unknown[];
    limitations?: string[];
    clarification?: string;
    write?: {
      status?: string;
      task_id?: string;
      created?: boolean;
      reason?: string;
    } | null;
  };
  steps: {
    sequence: number;
    node: string;
    tool_name: string;
    status: string;
    output: Record<string, unknown>;
    evidence_refs: string[];
  }[];
};

export default function InvestigationPage({
  shop,
  products,
}: {
  shop: Shop;
  products: Product[];
}) {
  const [job, setJob] = useState<Job | null>(null);
  const [runId, setRunId] = useState<string>();
  const [clarification, setClarification] = useState("");
  const start = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      request<Job>("investigations/", "POST", {
        shop: shop.id,
        ...data,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: (value) => {
      setRunId(undefined);
      setJob(value);
    },
  });
  const jobQuery = useQuery({
    queryKey: ["investigation-job", job?.id],
    queryFn: () => request<Job>(`jobs/${job?.id}/`),
    enabled:
      !!job?.id &&
      ["queued", "running", "retry_wait"].includes(job?.status ?? ""),
    refetchInterval: (query) =>
      ["queued", "running", "retry_wait"].includes(
        query.state.data?.status ?? "queued",
      )
        ? 1000
        : false,
  });
  const activeJob = jobQuery.data ?? job;
  const resultId =
    runId ?? (activeJob?.result?.investigation_id as string | undefined);
  const run = useQuery({
    queryKey: ["investigation", resultId],
    queryFn: () => request<Run>(`investigations/${resultId}/`),
    enabled: !!resultId,
    refetchInterval: (query) =>
      ["queued", "running"].includes(query.state.data?.status ?? "queued")
        ? 1000
        : false,
  });
  const cancel = useMutation({
    mutationFn: () =>
      request<Run>(`investigations/${resultId}/cancel/`, "POST"),
    onSuccess: () => void run.refetch(),
  });
  const resume = useMutation({
    mutationFn: () =>
      request<Job>(`investigations/${resultId}/resume/`, "POST"),
    onMutate: () => setRunId(resultId),
    onSuccess: (value) => {
      setJob(value);
      void run.refetch();
    },
  });
  const clarify = useMutation({
    mutationFn: (input: { query: string; child_asin?: string }) =>
      request<Job>(`investigations/${resultId}/clarify/`, "POST", {
        query: input.query,
        child_asin: input.child_asin ?? "",
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: (value) => {
      setJob(value);
      setClarification("");
      setRunId(undefined);
    },
  });
  const options = products.map((p) => ({
    value: p.child_asin,
    label: `${p.child_asin} / ${p.seller_sku}`,
  }));
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>自主调查</Typography.Title>
          <p>
            输入模糊问题后，系统在当前店铺授权范围内定位对象，按有界调查图选择指标和评论证据，并保存每一步。只有明确允许时，调查图才会通过领域命令创建内部核查草稿；它不会执行 Amazon 或 ERP 的外部经营变更。
          </p>
        </div>
      </div>
      <Card title="开始一次有界调查" style={{ marginBottom: 20 }}>
        <Form
          layout="vertical"
          onFinish={(data) => start.mutate(data)}
          initialValues={{ max_steps: 6 }}
        >
          <Form.Item
            name="query"
            label="问题"
            rules={[{ required: true, min: 2 }]}
          >
            <Input.TextArea
              rows={3}
              placeholder="例如：B000TEST01 最近退货异常的原因是什么？"
            />
          </Form.Item>
          <Space wrap>
            <Form.Item name="child_asin" label="子 ASIN（可选）">
              <Select
                allowClear
                showSearch
                style={{ width: 260 }}
                options={options}
                placeholder="不填则从问题和授权商品定位"
              />
            </Form.Item>
            <Form.Item name="max_steps" label="最大步骤">
              <InputNumber min={3} max={12} />
            </Form.Item>
            <Form.Item
              name="allow_internal_task"
              valuePropName="checked"
              style={{ marginTop: 30 }}
            >
              <Checkbox>允许调查完成后创建内部核查草稿</Checkbox>
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={start.isPending}>
              开始调查
            </Button>
          </Space>
        </Form>
        {start.error && (
          <Alert type="error" showIcon title={start.error.message} />
        )}
        {activeJob && (
          <Alert
            style={{ marginTop: 12 }}
            type={activeJob.status === "failed" ? "error" : "info"}
            showIcon
            title={`后台任务：${activeJob.status}${activeJob.error ? `；${activeJob.error}` : ""}`}
          />
        )}
      </Card>
      {(cancel.error || resume.error || clarify.error || run.error) && (
        <Alert
          type="error"
          showIcon
          title={(cancel.error || resume.error || clarify.error || run.error)?.message}
        />
      )}
      {run.isLoading && <Spin tip="正在读取调查状态" />}
      {run.data ? (
        <>
          <RefreshHistory
            kind="investigations"
            id={run.data.id}
            onOpen={setRunId}
          />
          <RunDetail
            run={run.data}
            onCancel={() => cancel.mutate()}
            onResume={() => resume.mutate()}
            onClarify={(asin) => {
              clarify.mutate({ query: clarification, child_asin: asin });
            }}
            clarification={clarification}
            setClarification={setClarification}
            busy={cancel.isPending || resume.isPending || clarify.isPending}
          />
        </>
      ) : (
        !run.isLoading && !activeJob && <Empty description="尚未开始调查" />
      )}
    </>
  );
}

function RunDetail({
  run,
  onCancel,
  onResume,
  onClarify,
  clarification,
  setClarification,
  busy,
}: {
  run: Run;
  onCancel: () => void;
  onResume: () => void;
  onClarify: (asin?: string) => void;
  clarification: string;
  setClarification: (value: string) => void;
  busy: boolean;
}) {
  const [selectedAsin, setSelectedAsin] = useState<string>();
  return (
    <Space direction="vertical" style={{ width: "100%" }}>
      <Card
        title={
          <Space>
            <span>调查状态</span>
            <Tag
              color={
                run.status === "completed"
                  ? "green"
                  : run.status === "needs_clarification"
                    ? "orange"
                    : "blue"
              }
            >
              {run.status}
            </Tag>
            <span>
              {run.steps_used} / {run.max_steps} 步
            </span>
            <Button
              size="small"
              danger
              disabled={!["queued", "running"].includes(run.status)}
              loading={busy}
              onClick={onCancel}
            >
              取消
            </Button>
            <Button
              size="small"
              disabled={!["cancelled", "partial"].includes(run.status)}
              loading={busy}
              onClick={onResume}
            >
              接续
            </Button>
          </Space>
        }
      >
        {run.budget && (
          <Alert
            type="info"
            showIcon
            message={`调查预算：剩余 ${run.budget.remaining_units} 单位；模型尝试 ${run.budget.model_attempts} 次；token ${run.budget.model_tokens}；累计 ${run.budget.elapsed_ms} ms；重复查询 ${run.budget.duplicate_queries} 次`}
          />
        )}
        {run.tool_permissions && (
          <Card size="small" title="调查工具权限矩阵" style={{ marginBottom: 12 }}>
            <Table
              size="small"
              rowKey="tool_name"
              pagination={false}
              dataSource={run.tool_permissions}
              columns={[
                { title: "工具", dataIndex: "tool_name" },
                { title: "能力", dataIndex: "description" },
                { title: "模式", dataIndex: "mode" },
                { title: "允许角色", dataIndex: "roles", render: (roles: string[]) => roles.join("、") },
                { title: "外部写入", dataIndex: "external_write", render: (value: boolean) => value ? "允许" : "禁止" },
              ]}
            />
          </Card>
        )}
        {run.status === "needs_clarification" && (
          <Card size="small" title="需要补充定位" style={{ marginBottom: 12 }}>
            <p>{run.stop_reason}</p>
            <Space wrap>
              <Select
                allowClear
                placeholder="选择授权范围内的候选 ASIN"
                style={{ minWidth: 260 }}
                value={selectedAsin}
                onChange={(value) => {
                  setSelectedAsin(value);
                }}
                options={(run.candidates ?? []).map((candidate) => ({
                  value: candidate.child_asin,
                  label: `${candidate.child_asin} / ${candidate.seller_sku}`,
                }))}
              />
              <Input
                value={clarification}
                onChange={(event) => setClarification(event.target.value)}
                placeholder="补充 SKU、名称或范围"
                style={{ width: 260 }}
              />
              <Button
                type="primary"
                disabled={clarification.trim().length < 2}
                loading={busy}
                onClick={() => onClarify(selectedAsin)}
              >
                澄清后继续
              </Button>
            </Space>
          </Card>
        )}
        {run.decisions?.length ? (
          <Collapse
            size="small"
            items={run.decisions.map((decision, index) => ({
              key: String(index),
              label: `${decision.phase ?? "决策"}：${decision.next_tool || "停止"}`,
              children: (
                <Typography.Paragraph>
                  {decision.hypothesis || decision.stop_reason || "已记录结构化决策"}
                  {decision.counter_evidence_query ? `；反证：${decision.counter_evidence_query}` : ""}
                </Typography.Paragraph>
              ),
            }))}
          />
        ) : null}
        {run.tool_calls?.length ? (
          <Typography.Paragraph type="secondary">
            已调用工具：{run.tool_calls.map((call) => `${call.tool_name}（${call.authorization}）`).join("、")}
          </Typography.Paragraph>
        ) : null}
        <Typography.Paragraph>
          {run.stop_reason || "执行中"}
        </Typography.Paragraph>
        {run.result.clarification && (
          <Alert type="warning" showIcon title={run.result.clarification} />
        )}
      </Card>
      <Collapse
        items={run.steps.map((step) => ({
          key: step.sequence,
          label: `${step.sequence}. ${step.node}${step.tool_name ? ` · ${step.tool_name}` : ""}`,
          children: (
            <>
              <pre className="wb-json">
                {JSON.stringify(step.output, null, 2)}
              </pre>
              {step.evidence_refs.length > 0 && (
                <Typography.Text type="secondary">
                  证据引用：{step.evidence_refs.join(", ")}
                </Typography.Text>
              )}
            </>
          ),
        }))}
      />
      <Card title="结论与边界">
        <Typography.Paragraph>
          假设：{(run.result.hypotheses ?? []).length} 条；证据：
          {(run.result.evidence ?? []).length} 条。
        </Typography.Paragraph>
        <ul>
          {(run.result.limitations ?? []).map((item) => (
            <li key={item}>{item}</li>
          ))}
        </ul>
      </Card>
      {run.result.write && (
        <Card title="调查产物写入">
          {run.result.write.status === "created" || run.result.write.status === "reused" ? (
            <Alert
              type="success"
              showIcon
              title={run.result.write.status === "created" ? "已创建内部核查草稿" : "已复用既有内部核查草稿"}
              description={`任务编号：${run.result.write.task_id ?? "未知"}`}
            />
          ) : (
            <Alert
              type="info"
              showIcon
              title="本次调查未写入内部任务"
              description={run.result.write.reason ?? "没有满足安全写入条件。"}
            />
          )}
        </Card>
      )}
    </Space>
  );
}
