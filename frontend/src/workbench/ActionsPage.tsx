import { useState } from "react";
import {
  Alert,
  Button,
  Card,
  Checkbox,
  Empty,
  Form,
  Input,
  Modal,
  Select,
  Space,
  Table,
  Tag,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Page, type Product, type Shop } from "./api";

type Task = {
  id: string;
  child_asin: string;
  title: string;
  action_type: string;
  question: string;
  completion_criteria: string;
  evidence_refs: string[];
  observed_metrics: string[];
  is_formal: boolean;
  product: string | null;
  assigned_to: number | null;
  status: string;
  due_at: string | null;
  review_status: string;
  review_result: string;
  review_comparison: Record<string, {
    baseline: string | number | null;
    current: string | number | null;
    unit: string;
    window_start: string;
    window_end: string;
    snapshot: string;
    delta?: string | number | null;
    method?: string;
    maturity?: {
      status: string;
      immature_orders?: number;
      unverified_first_shipment_orders?: number;
    };
  }>;
  version: number;
  created_at: string;
};
type Proposal = {
  id: string;
  child_asin: string;
  operation: string;
  before: Record<string, unknown>;
  after: Record<string, unknown>;
  status: string;
  review_note: string;
  version: number;
  payload_hash: string;
  approved_payload_hash: string;
  expires_at: string | null;
  execution_policy: string;
  execution_result: Record<string, unknown>;
  manual_authorization: {
    status: string;
    authorized_by: string;
    authorized_at: string;
    handoff_ref: string;
    note: string;
    system_execution: string;
  } | null;
  created_at: string;
};

const operationLabels: Record<string, string> = {
  internal_followup: "内部指派",
  price_change: "价格变更",
  inventory_change: "库存变更",
  listing_change: "Listing 变更",
};

const statusLabels: Record<string, string> = {
  pending: "待审核",
  approved: "已批准",
  rejected: "已拒绝",
  expired: "已过期",
  succeeded: "已完成",
  manual_authorized: "已授权人工处理",
};

function statusColor(status: string) {
  return {
    pending: "gold",
    approved: "blue",
    rejected: "red",
    expired: "red",
    succeeded: "green",
    manual_authorized: "green",
  }[status] ?? "default";
}

function formatDelta(value: string | number | null | undefined) {
  if (value === null || value === undefined || value === "") return "未知";
  if (typeof value === "number") return value > 0 ? `+${value}` : String(value);
  return value.startsWith("-") ? value : `+${value}`;
}

export default function ActionsPage({
  shop,
  products,
  canEdit,
}: {
  shop: Shop;
  products: Product[];
  canEdit: boolean;
}) {
  const cache = useQueryClient();
  const [form] = Form.useForm();
  const [reviewing, setReviewing] = useState<Task | null>(null);
  const [reviewMode, setReviewMode] = useState<"execute" | "review" | null>(null);
  const [editing, setEditing] = useState<Task | null>(null);
  const [draftForm] = Form.useForm();
  const [proposalForm] = Form.useForm();
  const [proposalOperation, setProposalOperation] = useState("internal_followup");
  const members = useQuery({
    queryKey: ["action-members", shop.id],
    queryFn: () =>
      request<{ items: { id: number; username: string }[] }>(
        `shops/${shop.id}/members/`,
      ),
  });
  const tasks = useQuery({
    queryKey: ["action-tasks", shop.id],
    queryFn: () => request<Page<Task>>(`action-tasks/?shop=${shop.id}`),
  });
  const reviewSnapshots = useQuery({
    queryKey: ["review-snapshots", shop.id, reviewing?.id],
    queryFn: () =>
      request<
        Page<{
          id: string;
          local_date: string;
          rule_version: string;
          payload: Record<string, unknown>;
        }>
      >(
        `snapshots/?shop=${shop.id}&child_asin=${reviewing?.child_asin}&product=${reviewing?.product ?? "aggregate"}`,
      ),
    enabled: !!reviewing,
  });
  const proposals = useQuery({
    queryKey: ["proposals", shop.id],
    queryFn: () => request<Page<Proposal>>(`change-proposals/?shop=${shop.id}`),
  });
  const create = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      request<Task>("action-tasks/", "POST", { shop: shop.id, ...data }),
    onSuccess: (task) => {
      form.resetFields();
      cache.setQueryData<Page<Task>>(["action-tasks", shop.id], (current) =>
        current
          ? {
              ...current,
              count: current.count + (current.results.some((item) => item.id === task.id) ? 0 : 1),
              results: [task, ...current.results.filter((item) => item.id !== task.id)],
            }
          : current,
      );
      void cache.invalidateQueries({ queryKey: ["action-tasks", shop.id] });
    },
  });
  const complete = useMutation({
    mutationFn: ({
      task,
      values,
    }: {
      task: Task;
      values: Record<string, unknown>;
    }) =>
      request<Task>(`action-tasks/${task.id}/complete/`, "POST", {
        ...values,
        expected_version: task.version,
      }),
    onSuccess: () => {
      setReviewing(null);
      void cache.invalidateQueries({ queryKey: ["action-tasks", shop.id] });
    },
  });
  const review = useMutation({
    mutationFn: ({
      task,
      values,
    }: {
      task: Task;
      values: Record<string, unknown>;
    }) =>
      request<Task>(`action-tasks/${task.id}/review/`, "POST", {
        ...values,
        expected_version: task.version,
      }),
    onSuccess: () => {
      setReviewing(null);
      void cache.invalidateQueries({ queryKey: ["action-tasks", shop.id] });
    },
  });
  const revise = useMutation({
    mutationFn: ({ task, values }: { task: Task; values: Record<string, unknown> }) =>
      request<Task>(`action-tasks/${task.id}/revise-draft/`, "POST", {
        ...values,
        expected_version: task.version,
      }),
    onSuccess: () => {
      setEditing(null);
      draftForm.resetFields();
      void cache.invalidateQueries({ queryKey: ["action-tasks", shop.id] });
    },
  });
  const createProposal = useMutation({
    mutationFn: (values: Record<string, unknown>) => {
      const operation = String(values.operation);
      let target: Record<string, unknown>;
      let before: Record<string, unknown>;
      let after: Record<string, unknown>;
      if (operation === "internal_followup") {
        const task = (tasks.data?.results ?? []).find((item) => item.id === values.target_task);
        if (!task) throw new Error("请选择一个开放任务作为内部指派目标。");
        if (!values.assigned_to || !values.due_at) {
          throw new Error("内部指派需要负责人和未来截止时间。");
        }
        target = { action_task: task.id, expected_version: task.version };
        before = { assigned_to: task.assigned_to, due_at: task.due_at };
        after = {
          assigned_to: Number(values.assigned_to),
          due_at: new Date(String(values.due_at)).toISOString(),
        };
      } else {
        const parse = (name: string) => {
          try {
            const parsed = JSON.parse(String(values[name] ?? "{}"));
            if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
              throw new Error();
            }
            return parsed as Record<string, unknown>;
          } catch {
            throw new Error(`${name === "target_json" ? "目标" : name === "before_json" ? "变更前" : "变更后"}必须是 JSON 对象。`);
          }
        };
        target = parse("target_json");
        before = parse("before_json");
        after = parse("after_json");
      }
      return request<Proposal>("change-proposals/", "POST", {
        shop: shop.id,
        child_asin: values.child_asin,
        operation,
        target,
        before,
        after,
        evidence_refs: values.evidence_refs ?? [],
        idempotency_key: values.idempotency_key,
      });
    },
    onSuccess: (proposal) => {
      proposalForm.resetFields(["target_task", "assigned_to", "due_at", "target_json", "before_json", "after_json", "evidence_refs"]);
      cache.setQueryData<Page<Proposal>>(["proposals", shop.id], (current) =>
        current
          ? {
              ...current,
              count: current.count + (current.results.some((item) => item.id === proposal.id) ? 0 : 1),
              results: [proposal, ...current.results.filter((item) => item.id !== proposal.id)],
            }
          : current,
      );
      void cache.invalidateQueries({ queryKey: ["proposals", shop.id] });
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
          <Typography.Title level={2}>动作与复查</Typography.Title>
          <p>
            执行记录、复查结论和变更提案分开保存；批准提案不会直接写入 Amazon 或
            ERP。
          </p>
        </div>
      </div>
      {create.error && (
        <Alert type="error" showIcon title={create.error.message} />
      )}
      {createProposal.error && (
        <Alert type="error" showIcon title={createProposal.error.message} />
      )}
      <Card title="创建待核查任务" style={{ marginBottom: 20 }}>
        <Form
          form={form}
          name="action-create"
          layout="inline"
          onFinish={(data) =>
            create.mutate({
              ...data,
              due_at: data.due_at
                ? new Date(data.due_at).toISOString()
                : undefined,
            })
          }
        >
          <Form.Item
            name="child_asin"
            label="子 ASIN"
            rules={[{ required: true }]}
          >
            <Select showSearch style={{ width: 250 }} options={options} />
          </Form.Item>
          <Form.Item name="title" label="任务" rules={[{ required: true }]}>
            <Input
              style={{ width: 300 }}
              placeholder="例如：核对退货原因并记录证据"
            />
          </Form.Item>
          <Form.Item name="question" label="核查问题">
            <Input />
          </Form.Item>
          <Form.Item name="completion_criteria" label="完成标准">
            <Input />
          </Form.Item>
          <Form.Item name="observed_metrics" label="观察指标">
            <Select
              mode="multiple"
              style={{ minWidth: 180 }}
              options={[
                { value: "ordered_units", label: "订购件数" },
                { value: "sessions", label: "Sessions" },
                { value: "unit_session_percent", label: "件数／会话" },
                { value: "refunds", label: "成熟退款订单率" },
                { value: "returns", label: "成熟退货订单率" },
              ]}
            />
          </Form.Item>
          <Form.Item name="assigned_to" label="负责人">
            <Select
              allowClear
              style={{ minWidth: 160 }}
              options={members.data?.items.map((m) => ({
                value: m.id,
                label: m.username,
              }))}
            />
          </Form.Item>
          <Form.Item name="due_at" label="截止时间">
            <Input type="datetime-local" />
          </Form.Item>
          <Form.Item name="is_formal" valuePropName="checked">
            <Checkbox>正式指派</Checkbox>
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={create.isPending}>
            建立任务
          </Button>
        </Form>
      </Card>
      <Card
        title="创建变更提案"
        style={{ marginBottom: 20 }}
        extra={<Tag color="blue">先审核，再按策略处理</Tag>}
      >
        <Form
          form={proposalForm}
          layout="vertical"
          initialValues={{
            operation: "internal_followup",
            idempotency_key: `proposal-${Date.now()}`,
          }}
          onFinish={(values) => createProposal.mutate(values)}
        >
          <Space wrap align="start" style={{ width: "100%" }}>
            <Form.Item name="child_asin" label="子 ASIN" rules={[{ required: true }]}>
              <Select showSearch style={{ width: 250 }} options={options} />
            </Form.Item>
            <Form.Item name="operation" label="提案类型" rules={[{ required: true }]}>
              <Select
                style={{ width: 190 }}
                options={Object.entries(operationLabels).map(([value, label]) => ({ value, label }))}
                onChange={(value) => setProposalOperation(value)}
              />
            </Form.Item>
            <Form.Item name="idempotency_key" label="提案编号" rules={[{ required: true }]}>
              <Input style={{ width: 220 }} />
            </Form.Item>
          </Space>
          {proposalOperation === "internal_followup" ? (
            <>
              <Alert
                type="info"
                showIcon
                title="内部指派由系统在批准后执行；它只会更新本系统中的任务负责人和截止时间。"
                style={{ marginBottom: 16 }}
              />
              <Space wrap align="start">
                <Form.Item name="target_task" label="开放任务" rules={[{ required: true }]}>
                  <Select
                    showSearch
                    optionFilterProp="label"
                    style={{ width: 340 }}
                    options={(tasks.data?.results ?? [])
                      .filter((task) => task.status === "open" && !task.is_formal)
                      .map((task) => ({ value: task.id, label: `${task.title} · ${task.child_asin} · v${task.version}` }))}
                  />
                </Form.Item>
                <Form.Item name="assigned_to" label="新负责人" rules={[{ required: true }]}>
                  <Select
                    style={{ width: 180 }}
                    options={members.data?.items.map((member) => ({ value: member.id, label: member.username }))}
                  />
                </Form.Item>
                <Form.Item name="due_at" label="截止时间" rules={[{ required: true }]}>
                  <Input type="datetime-local" />
                </Form.Item>
              </Space>
            </>
          ) : (
            <>
              <Alert
                type="warning"
                showIcon
                title="价格、库存和 Listing 提案只记录人工经营变更授权，系统不会连接 Amazon 或 ERP，也不会替你执行。"
                style={{ marginBottom: 16 }}
              />
              <Space direction="vertical" style={{ width: "100%" }}>
                <Form.Item name="target_json" label="结构化目标" rules={[{ required: true }]}>
                  <Input.TextArea rows={2} placeholder='例如：{"field":"price","currency":"USD"}' />
                </Form.Item>
                <Form.Item name="before_json" label="变更前值" rules={[{ required: true }]}>
                  <Input.TextArea rows={2} placeholder='例如：{"value":"20.00"}' />
                </Form.Item>
                <Form.Item name="after_json" label="变更后值" rules={[{ required: true }]}>
                  <Input.TextArea rows={2} placeholder='例如：{"value":"18.00"}' />
                </Form.Item>
              </Space>
            </>
          )}
          <Form.Item name="evidence_refs" label="依据编号">
            <Select mode="tags" placeholder="snapshot:...、report:... 或 retrieval:..." />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={createProposal.isPending}>
            保存变更提案
          </Button>
        </Form>
      </Card>
      <Table<Task>
        rowKey="id"
        dataSource={tasks.data?.results ?? []}
        loading={tasks.isLoading}
        pagination={{ pageSize: 50, showSizeChanger: false }}
        locale={{ emptyText: <Empty description="暂无待核查任务" /> }}
        columns={[
          { title: "任务", dataIndex: "title" },
          { title: "子 ASIN", dataIndex: "child_asin" },
          {
            title: "状态",
            render: (_, t) => (
              <Tag color={t.status === "completed" ? "green" : "gold"}>
                {t.status}
              </Tag>
            ),
          },
          { title: "复查", render: (_, t) => t.review_status },
          {
            title: "复查结论",
            render: (_, t) => (
              <Space direction="vertical" size={2}>
                <span>{t.review_result || "待记录"}</span>
                {Object.entries(t.review_comparison ?? {}).map(([metric, value]) => (
                  <Typography.Text type="secondary" key={metric}>
                    {metric}：{value.baseline ?? "未知"} → {value.current ?? "未知"} {value.unit}；
                    变化 {formatDelta(value.delta)}；{value.window_start} 至 {value.window_end}；
                    方法 {value.method ?? "未注明"}；成熟度 {value.maturity?.status ?? "未知"}；
                    快照 {value.snapshot.slice(0, 8)}
                  </Typography.Text>
                ))}
              </Space>
            ),
          },
          {
            title: "创建时间",
            render: (_, t) => new Date(t.created_at).toLocaleString("zh-CN"),
          },
          {
            title: "操作",
            render: (_, t) => (
              <Space>
                {t.status === "open" && (
                  <Button
                    type="link"
                    onClick={() => {
                    setEditing(t);
                      draftForm.setFieldsValue({
                        title: t.title,
                        action_type: t.action_type,
                        question: t.question,
                        completion_criteria: t.completion_criteria,
                        evidence_refs: t.evidence_refs,
                        observed_metrics: t.observed_metrics,
                      });
                    }}
                  >
                    修订草稿
                  </Button>
                )}
                <Button
                  disabled={t.status === "completed"}
                  onClick={() => {
                    setReviewMode("execute");
                    setReviewing(t);
                  }}
                >
                  记录执行
                </Button>
                <Button
                  type="link"
                  onClick={() => {
                    setReviewMode("review");
                    setReviewing(t);
                  }}
                >
                  安排复查
                </Button>
              </Space>
            ),
          },
        ]}
      />
      <Typography.Title level={4} style={{ marginTop: 24 }}>
        变更提案
      </Typography.Title>
      <Table<Proposal>
        rowKey="id"
        dataSource={proposals.data?.results ?? []}
        loading={proposals.isLoading}
        pagination={{ pageSize: 50, showSizeChanger: false }}
        locale={{ emptyText: <Empty description="暂无变更提案" /> }}
        columns={[
          { title: "提案编号", dataIndex: "idempotency_key" },
          { title: "子 ASIN", dataIndex: "child_asin" },
          { title: "操作", render: (_, p) => operationLabels[p.operation] ?? p.operation },
          {
            title: "状态",
            render: (_, p) => (
              <Space size={4}>
                <Tag color={statusColor(p.status)}>{statusLabels[p.status] ?? p.status}</Tag>
                <Tag>{p.execution_policy === "internal_task_assignment" ? "内部可执行" : "人工授权"}</Tag>
              </Space>
            ),
          },
          {
            title: "执行边界",
            render: (_, p) => (
              <Space direction="vertical" size={0}>
                <Typography.Text>{p.execution_policy === "internal_task_assignment" ? "系统仅处理内部指派" : "需人工外部授权"}</Typography.Text>
                {p.expires_at && <Typography.Text type="secondary">有效至 {new Date(p.expires_at).toLocaleString("zh-CN")}</Typography.Text>}
                {p.manual_authorization && <Tag color="green">已交接 {p.manual_authorization.handoff_ref}</Tag>}
              </Space>
            ),
          },
          {
            title: "创建时间",
            render: (_, p) => new Date(p.created_at).toLocaleString("zh-CN"),
          },
          {
            title: "审核",
            render: (_, p) =>
              canEdit &&
              ["pending", "approved", "rejected", "expired", "succeeded", "manual_authorized"].includes(p.status) ? (
                <ProposalReview
                  proposal={p}
                  onDone={() =>
                    void cache.invalidateQueries({
                      queryKey: ["proposals", shop.id],
                    })
                  }
                />
              ) : (
                p.review_note || "—"
              ),
          },
        ]}
      />
      <Modal
        open={!!reviewing && reviewMode === "execute"}
        title="记录任务执行"
        onCancel={() => {
          setReviewing(null);
          setReviewMode(null);
        }}
        footer={null}
      >
        {complete.error && (
          <Alert type="error" title={complete.error.message} />
        )}
        <Form
          name="action-execution"
          onFinish={(values) =>
            complete.mutate({
              task: reviewing!,
              values: {
                ...values,
                effective_at: values.effective_at
                  ? new Date(values.effective_at).toISOString()
                  : undefined,
                review_at: values.review_at
                  ? new Date(values.review_at).toISOString()
                  : undefined,
              },
            })
          }
        >
          <Form.Item name="notes" rules={[{ required: true }]}>
            <Input.TextArea rows={5} placeholder="记录实际核查结果和证据引用" />
          </Form.Item>
          <Form.Item name="effective_at" label="实际生效时间">
            <Input type="datetime-local" />
          </Form.Item>
          <Form.Item name="review_at" label="复查时间">
            <Input type="datetime-local" />
          </Form.Item>
          <Form.Item name="observed_metrics" label="观察指标">
            <Select
              mode="multiple"
              options={[
                { value: "ordered_units", label: "订购件数" },
                { value: "sessions", label: "Sessions" },
                { value: "unit_session_percent", label: "件数／会话" },
                { value: "refunds", label: "成熟退款订单率" },
                { value: "returns", label: "成熟退货订单率" },
              ]}
            />
          </Form.Item>
          <Form.Item name="evidence_refs" label="执行证据编号">
            <Select mode="tags" />
          </Form.Item>

          <Button type="primary" htmlType="submit" loading={complete.isPending}>
            保存执行记录
          </Button>
        </Form>
      </Modal>
      <Modal
        open={!!editing}
        title="修订任务草稿"
        onCancel={() => setEditing(null)}
        footer={null}
        destroyOnHidden
      >
        {revise.error && <Alert type="error" showIcon title={revise.error.message} />}
        <Form
          form={draftForm}
          layout="vertical"
          onFinish={(values) => revise.mutate({ task: editing!, values })}
        >
          <Form.Item name="title" label="任务标题" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item name="question" label="核查问题"><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="completion_criteria" label="完成标准"><Input.TextArea rows={3} /></Form.Item>
          <Form.Item name="evidence_refs" label="证据编号"><Select mode="tags" /></Form.Item>
          <Button type="primary" htmlType="submit" loading={revise.isPending}>保存草稿</Button>
        </Form>
      </Modal>
      <Modal
        open={!!reviewing && reviewMode === "review"}
        title="安排或完成复查"
        onCancel={() => {
          setReviewing(null);
          setReviewMode(null);
        }}
        footer={null}
      >
        {reviewing?.review_status === "completed" && reviewing.review_result && (
          <Alert
            type="success"
            showIcon
            title="复查已完成"
            description={reviewing.review_result}
            style={{ marginBottom: 16 }}
          />
        )}
        {reviewing?.review_status === "completed" && Object.keys(reviewing.review_comparison ?? {}).length > 0 && (
          <Card size="small" title="前后指标对比" style={{ marginBottom: 16 }}>
            {Object.entries(reviewing.review_comparison).map(([metric, value]) => (
              <Typography.Paragraph key={metric}>
                {metric}：{value.baseline ?? "未知"} → {value.current ?? "未知"} {value.unit}；
                变化 {formatDelta(value.delta)}；观察窗口 {value.window_start} 至 {value.window_end}；
                方法 {value.method ?? "未注明"}；成熟度 {value.maturity?.status ?? "未知"}；快照 {value.snapshot}
              </Typography.Paragraph>
            ))}
          </Card>
        )}
        {review.error && <Alert type="error" title={review.error.message} />}
        <Form
          name="action-review"
          onFinish={(values) =>
            review.mutate({
              task: reviewing!,
              values: {
                ...values,
                review_at: values.review_at
                  ? new Date(values.review_at).toISOString()
                  : undefined,
              },
            })
          }
        >
          <Form.Item name="status" label="复查状态" initialValue="scheduled">
            <Select
              options={[
                { value: "scheduled", label: "已安排" },
                { value: "completed", label: "已完成" },
                { value: "not_scheduled", label: "未安排" },
              ]}
            />
          </Form.Item>
          <Form.Item name="review_at" label="复查时间">
            <Input type="datetime-local" />
          </Form.Item>
          <Form.Item name="snapshot" label="复查指标快照">
            <Select
              showSearch
              optionFilterProp="label"
              options={reviewSnapshots.data?.results
                .filter((s) => s.payload.mode !== "historical")
                .map((s) => ({
                  value: s.id,
                  label: `${s.local_date} · ${s.rule_version}`,
                }))}
            />
          </Form.Item>
          <Form.Item name="result" label="复查结论">
            <Input.TextArea rows={4} />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={review.isPending}>
            保存复查
          </Button>
        </Form>
      </Modal>
    </>
  );
}

function ProposalReview({
  proposal,
  onDone,
}: {
  proposal: Proposal;
  onDone: () => void;
}) {
  const [open, setOpen] = useState(false);
  const cache = useQueryClient();
  const review = useMutation({
    mutationFn: (values: { decision: string; note: string }) =>
      request(`change-proposals/${proposal.id}/review/`, "POST", {
        ...values,
        expected_version: proposal.version,
        payload_hash: proposal.payload_hash,
      }),
    onSuccess: () => {
      setOpen(false);
      onDone();
    },
  });
  const execute = useMutation({
    mutationFn: () =>
      request<Proposal>(`change-proposals/${proposal.id}/execute/`, "POST", {
        expected_version: proposal.version,
        payload_hash: proposal.approved_payload_hash,
      }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["proposals"] });
      void cache.invalidateQueries({ queryKey: ["action-tasks"] });
    },
  });
  const authorizeManual = useMutation({
    mutationFn: (values: { note: string; handoff_ref: string }) =>
      request<Proposal>(`change-proposals/${proposal.id}/authorize-manual/`, "POST", {
        ...values,
        expected_version: proposal.version,
      }),
    onSuccess: () => {
      setOpen(false);
      onDone();
    },
  });
  const isExternal = proposal.execution_policy !== "internal_task_assignment";
  const canReview = proposal.status === "pending";
  const canExecute = proposal.operation === "internal_followup" && proposal.status === "approved";
  const canAuthorize = isExternal && proposal.status === "approved" && !proposal.manual_authorization;
  return (
    <>
      <Button
        size="small"
        onClick={() => {
          review.reset();
          setOpen(true);
        }}
      >
        查看并审核
      </Button>
      <Modal
        open={open}
        title="核对变更提案"
        onCancel={() => setOpen(false)}
        footer={null}
        destroyOnHidden
      >
        <Alert
          type={isExternal ? "warning" : "info"}
          title={
            isExternal
              ? "这是外部经营变更提案。批准和人工授权只形成可追溯交接记录，系统永远不会修改 Amazon 或 ERP。"
              : "批准只记录提案审核结果；内部指派由系统按当前任务版本执行。"
          }
        />
        {canExecute && (
            <Button
              onClick={() => execute.mutate()}
              loading={execute.isPending}
            >
              执行已批准的内部指派
            </Button>
        )}
        {canAuthorize && (
          <Form
            layout="vertical"
            onFinish={(values) => authorizeManual.mutate(values)}
            style={{ marginTop: 16 }}
          >
            <Form.Item
              name="handoff_ref"
              label="人工交接编号"
              rules={[{ required: true, pattern: /^[A-Za-z0-9._-]+$/ }]}
            >
              <Input placeholder="例如 SC-PRICE-001" />
            </Form.Item>
            <Form.Item
              name="note"
              label="授权说明"
              rules={[{ required: true, message: "请记录人工执行范围和依据" }]}
            >
              <Input.TextArea rows={3} />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={authorizeManual.isPending}>
              记录人工授权（不执行外部变更）
            </Button>
          </Form>
        )}
        {proposal.manual_authorization && (
          <Alert
            type="success"
            showIcon
            style={{ marginTop: 16 }}
            title={`已记录人工交接：${proposal.manual_authorization.handoff_ref}`}
            description={`${proposal.manual_authorization.authorized_by} · ${proposal.manual_authorization.note}`}
          />
        )}
        {execute.error && <Alert type="error" title={execute.error.message} />}
        {authorizeManual.error && <Alert type="error" title={authorizeManual.error.message} />}
        <Typography.Title level={5}>变更前</Typography.Title>
        <pre style={{ whiteSpace: "pre-wrap" }}>
          {JSON.stringify(proposal.before, null, 2)}
        </pre>
        <Typography.Title level={5}>变更后</Typography.Title>
        <pre style={{ whiteSpace: "pre-wrap" }}>
          {JSON.stringify(proposal.after, null, 2)}
        </pre>
        {review.error && (
          <Alert type="error" showIcon title={review.error.message} />
        )}
        {canReview && (
          <Form layout="vertical" onFinish={(values) => review.mutate(values)}>
            <Form.Item
              name="decision"
              label="审核决定"
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  { value: "approve", label: "批准" },
                  { value: "reject", label: "拒绝" },
                ]}
              />
            </Form.Item>
            <Form.Item
              name="note"
              label="审核依据"
              rules={[{ required: true, message: "请填写实际审核依据" }]}
            >
              <Input.TextArea maxLength={1000} rows={3} />
            </Form.Item>
            <Button type="primary" htmlType="submit" loading={review.isPending}>
              保存审核结果
            </Button>
          </Form>
        )}
        {proposal.status === "expired" && <Alert type="error" title="提案已过期，必须重新提交。" />}
      </Modal>
    </>
  );
}
