import RefreshHistory from "./RefreshHistory";
import InvestigationEvidence, {
  type InvestigationEvidenceResult,
} from "./InvestigationEvidence";
import { useState } from "react";
import {
  isActiveJob,
  jobStatusLabel,
  investigationStatus,
  observedInvestigationMode,
  investigationFailures,
  type ExecutionDecision,
  type BudgetEntry,
} from "./executionStatus";
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
    execution_origin?: ExecutionDecision["execution_origin"];
    model_receipt?: ExecutionDecision["model_receipt"];
  }[];
  counter_evidence?: unknown[];
  budget?: {
    remaining_units: number;
    model_attempts: number;
    model_tokens: number;
    model_cost: string;
    entries?: BudgetEntry[];
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
  result: InvestigationEvidenceResult & {
    hypotheses?: unknown[];
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

export default function InvestigationPage(props: {
  shop: Shop;
  products: Product[];
}) {
  return <InvestigationContent key={props.shop.id} {...props} />;
}

function InvestigationContent({
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
        planner_provider: data.planner_provider ?? "deterministic",
        idempotency_key: crypto.randomUUID(),
      }),
    onMutate: () => {
      setJob(null);
      setRunId(undefined);
      setClarification("");
    },
    onSuccess: (value) => {
      setRunId(undefined);
      setJob(value);
    },
  });
  const jobQuery = useQuery({
    queryKey: ["investigation-job", shop.id, job?.id],
    queryFn: () => request<Job>(`jobs/${job?.id}/`),
    enabled: (query) =>
      !!job?.id && isActiveJob(query.state.data?.status ?? job.status),
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) =>
      query.state.status !== "error" &&
      isActiveJob(query.state.data?.status ?? job?.status)
        ? 1000
        : false,
  });
  const activeJob = jobQuery.data ?? job;
  const resultId =
    runId ?? (activeJob?.result?.investigation_id as string | undefined);
  const run = useQuery({
    queryKey: ["investigation", shop.id, resultId],
    queryFn: () => request<Run>(`investigations/${resultId}/`),
    enabled: !!resultId,
    retry: false,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
    refetchInterval: (query) =>
      query.state.status !== "error" &&
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
            在当前授权店铺内定位问题，逐步核查指标与评论证据，并保留执行记录。
          </p>
        </div>
      </div>
      <Card
        className="wb-investigation-form"
        title="开始一次有界调查"
        style={{ marginBottom: 20 }}
      >
        <Form
          layout="vertical"
          onFinish={(data) => start.mutate(data)}
          initialValues={{ max_steps: 6, planner_provider: "deterministic" }}
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
          <div className="wb-investigation-grid">
            <Form.Item name="planner_provider" label="调查模式">
              <Select
                style={{ width: "100%" }}
                options={[
                  { value: "deterministic", label: "规则调查（默认）" },
                  { value: "deepseek", label: "DeepSeek 调查（需后端校验）" },
                ]}
              />
            </Form.Item>
            <Form.Item name="child_asin" label="子 ASIN（可选）">
              <Select
                allowClear
                showSearch
                style={{ width: "100%" }}
                options={options}
                placeholder="不填则从问题和授权商品定位"
              />
            </Form.Item>
            <Form.Item name="max_steps" label="最大步骤">
              <InputNumber min={3} max={12} />
            </Form.Item>
          </div>
          <p className="wb-field-note">
            DeepSeek
            可用性未确认，需通过后端供应商、区域、传输授权和预算校验，可能产生模型费用。规则调查仅指定规划模式；评论语义检索另受后端配置约束。
          </p>
          <div className="wb-form-footer">
            <Form.Item
              name="allow_internal_task"
              valuePropName="checked"
              style={{ marginBottom: 0 }}
            >
              <Checkbox>允许调查完成后创建内部核查草稿</Checkbox>
            </Form.Item>
            <Button
              type="primary"
              aria-label="开始调查"
              aria-busy={start.isPending}
              htmlType="submit"
              loading={start.isPending}
            >
              开始调查
            </Button>
          </div>
          <p className="wb-field-note">
            仅明确允许时创建内部核查草稿；不会执行 Amazon 或 ERP
            的外部经营变更。
          </p>
        </Form>
        {start.error && (
          <Alert type="error" showIcon title={start.error.message} />
        )}
        {activeJob && (
          <Alert
            style={{ marginTop: 12 }}
            type={activeJob.status === "failed" ? "error" : "info"}
            showIcon
            title={`后台任务：${jobStatusLabel(activeJob.status)}${activeJob.error ? `；${activeJob.error}` : ""}`}
          />
        )}
      </Card>
      {jobQuery.error && (
        <Alert
          type="error"
          showIcon
          title="任务状态读取失败，已暂停自动刷新"
          description={jobQuery.error.message}
          action={
            <Button onClick={() => void jobQuery.refetch()}>
              重新读取状态
            </Button>
          }
        />
      )}
      {run.error && (
        <Button onClick={() => void run.refetch()}>重新读取调查状态</Button>
      )}
      {(cancel.error || resume.error || clarify.error || run.error) && (
        <Alert
          type="error"
          showIcon
          title={
            (cancel.error || resume.error || clarify.error || run.error)
              ?.message
          }
        />
      )}
      {run.isLoading && <Spin description="正在读取调查状态" />}
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
  const failures = investigationFailures(run.budget?.entries);
  const displayStatus = investigationStatus(
    run.status,
    run.result.completion_status,
  );
  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      <Card
        title={
          <Space wrap className="wb-run-title">
            <span>调查状态</span>
            <Tag color={displayStatus.color}>{displayStatus.label}</Tag>
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
              接续（沿用原模式）
            </Button>
          </Space>
        }
      >
        <Typography.Paragraph>
          实际调查规划模式：{observedInvestigationMode(run.decisions)}
        </Typography.Paragraph>
        <Alert
          type={displayStatus.type}
          showIcon
          title={displayStatus.description}
        />
        <Typography.Paragraph type="secondary">
          实际模型扣款：未知（当前接口未提供币种和扣款确认）。
        </Typography.Paragraph>
        {failures.map((failure) => (
          <Alert
            key={failure.code}
            type="error"
            showIcon
            title={failure.message}
          />
        ))}
        {["partial", "cancelled", "needs_clarification"].includes(
          run.status,
        ) && (
          <Alert
            type="warning"
            showIcon
            title="接续或澄清沿用原调查模式与剩余预算；原模式可能涉及模型费用，请确认授权后操作。"
          />
        )}
        <Collapse
          size="small"
          items={[
            {
              key: "execution-details",
              label: "执行预算与工具权限（技术详情）",
              children: (
                <>
                  {run.budget && (
                    <Alert
                      type="info"
                      showIcon
                      title={`调查预算：剩余 ${run.budget.remaining_units} 单位；模型尝试 ${run.budget.model_attempts} 次；token ${run.budget.model_tokens}；累计 ${run.budget.elapsed_ms} ms；重复查询 ${run.budget.duplicate_queries} 次`}
                    />
                  )}
                  {run.tool_permissions && (
                    <Card
                      size="small"
                      title="调查工具权限矩阵"
                      style={{ marginBottom: 12 }}
                    >
                      <Table
                        size="small"
                        rowKey="tool_name"
                        pagination={false}
                        dataSource={run.tool_permissions}
                        columns={[
                          { title: "工具", dataIndex: "tool_name" },
                          { title: "能力", dataIndex: "description" },
                          { title: "模式", dataIndex: "mode" },
                          {
                            title: "允许角色",
                            dataIndex: "roles",
                            render: (roles: string[]) => roles.join("、"),
                          },
                          {
                            title: "外部写入",
                            dataIndex: "external_write",
                            render: (value: boolean) =>
                              value ? "允许" : "禁止",
                          },
                        ]}
                      />
                    </Card>
                  )}
                </>
              ),
            },
          ]}
        />
        {run.status === "needs_clarification" && (
          <Card size="small" title="需要补充定位" style={{ marginBottom: 12 }}>
            <p>{run.stop_reason}</p>
            <Space wrap>
              <Select
                allowClear
                placeholder="选择授权范围内的候选 ASIN"
                style={{ width: "min(260px, 100%)", minWidth: 0 }}
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
                style={{ width: "min(260px, 100%)" }}
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
                  {decision.hypothesis ||
                    decision.stop_reason ||
                    "已记录结构化决策"}
                  {decision.counter_evidence_query
                    ? `；反证：${decision.counter_evidence_query}`
                    : ""}
                </Typography.Paragraph>
              ),
            }))}
          />
        ) : null}
        {run.tool_calls?.length ? (
          <Typography.Paragraph type="secondary">
            已调用工具：
            {run.tool_calls
              .map((call) => `${call.tool_name}（${call.authorization}）`)
              .join("、")}
          </Typography.Paragraph>
        ) : null}
        <Typography.Paragraph>
          {run.stop_reason || displayStatus.label}
        </Typography.Paragraph>
        {run.result.clarification && (
          <Alert type="warning" showIcon title={run.result.clarification} />
        )}
      </Card>
      <Collapse
        data-testid="investigation-trajectory"
        items={[...run.steps]
          .sort((a, b) => a.sequence - b.sequence)
          .map((step) => ({
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
      <InvestigationEvidence result={run.result} runId={run.id} />
      {run.result.write && (
        <Card title="调查产物写入">
          {run.result.write.status === "created" ||
          run.result.write.status === "reused" ? (
            <Alert
              type="success"
              showIcon
              title={
                run.result.write.status === "created"
                  ? "已创建内部核查草稿"
                  : "已复用既有内部核查草稿"
              }
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
