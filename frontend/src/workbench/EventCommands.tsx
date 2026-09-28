import EventGrouping from "./EventGrouping";
import {
  Alert,
  Button,
  Checkbox,
  Form,
  Input,
  Select,
  Space,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Page } from "./api";
import type { components } from "./schema";

type Event = components["schemas"]["Event"];
type Task = components["schemas"]["ActionTask"];
type Evidence = components["schemas"]["Evidence"];
const labels: Record<string, string> = {
  pending: "待核查",
  processing: "处理中",
  observing: "待观察",
  closed: "已关闭",
};

export default function EventCommands({ eventId }: { eventId: string }) {
  const cache = useQueryClient();
  const [form] = Form.useForm();
  const operation = Form.useWatch("operation", form);
  const event = useQuery({
    queryKey: ["event", eventId],
    queryFn: () => request<Event>(`events/${eventId}/`),
  });
  const tasks = useQuery({
    queryKey: ["event-tasks", eventId],
    queryFn: () => request<Page<Task>>(`action-tasks/?event=${eventId}`),
  });
  const evidence = useQuery({
    queryKey: ["event-evidence", eventId],
    queryFn: () => request<Page<Evidence>>(`events/${eventId}/evidence/`),
  });
  const change = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      request<Event>(`events/${eventId}/command/`, "POST", {
        ...data,
        expected_version: event.data?.version,
        ...(data.snoozed_until
          ? {
              snoozed_until: new Date(String(data.snoozed_until)).toISOString(),
            }
          : {}),
      }),
    onSuccess: () => {
      form.resetFields();
      void cache.invalidateQueries({ queryKey: ["events"] });
      void cache.invalidateQueries({ queryKey: ["event", eventId] });
    },
  });
  const create = useMutation({
    mutationFn: (data: Record<string, unknown>) =>
      request<Task>("action-tasks/", "POST", {
        ...data,
        event: eventId,
        shop: event.data?.shop,
        child_asin: event.data?.child_asin,
        product: event.data?.product,
      }),
    onSuccess: () => {
      void cache.invalidateQueries({ queryKey: ["event-tasks", eventId] });
      void cache.invalidateQueries({ queryKey: ["action-tasks"] });
    },
  });
  if (!event.data)
    return event.error ? (
      <Alert type="error" title={event.error.message} />
    ) : null;
  return (
    <section style={{ marginBottom: 24 }}>
      <EventGrouping event={event.data} />
      <Typography.Title level={4}>
        处理事件 · {labels[event.data.status] ?? event.data.status}
      </Typography.Title>
      {event.data.snoozed_until && (
        <p>
          暂缓至 {new Date(event.data.snoozed_until).toLocaleString()}
          ，原状态与证据保留。
        </p>
      )}
      {change.error && <Alert type="error" title={change.error.message} />}
      {event.data.status === "closed" ? (
        <p>{event.data.closure_note}</p>
      ) : (
        <Form
          form={form}
          name={`event-command-${eventId}`}
          layout="vertical"
          onFinish={(values) => change.mutate(values)}
        >
          <Form.Item
            name="operation"
            label="处理操作"
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "claim", label: "领取或重新分派" },
                { value: "observe", label: "转入观察" },
                { value: "close", label: "关闭事件" },
                { value: "snooze", label: "暂缓提醒" },
                { value: "unsnooze", label: "解除暂缓" },
              ]}
            />
          </Form.Item>
          {operation === "claim" && (
            <Typography.Paragraph type="secondary">
              提交后由你负责；后续仍需记录实际执行与复查。
            </Typography.Paragraph>
          )}
          {operation === "observe" && (
            <Form.Item
              name="action_task"
              label="已完成动作"
              rules={[{ required: true }]}
            >
              <Select
                options={tasks.data?.results
                  .filter((t) => t.status === "completed")
                  .map((t) => ({ value: t.id, label: t.title }))}
              />
            </Form.Item>
          )}
          {operation === "close" && (
            <>
              <Form.Item
                name="closure_reason"
                label="关闭结论"
                rules={[{ required: true }]}
              >
                <Select
                  options={[
                    { value: "resolved", label: "已解决" },
                    { value: "false_positive", label: "误报" },
                    { value: "known_change", label: "已知变化" },
                    { value: "duplicate", label: "重复事件" },
                  ]}
                />
              </Form.Item>
              <Form.Item
                name="duplicate_of"
                label="重复事件编号（仅重复时填写）"
              >
                <Input />
              </Form.Item>
              <Form.Item
                name="evidence_refs"
                label="核查依据"
                rules={[{ required: true }]}
              >
                <Select
                  mode="multiple"
                  options={Array.from(
                    new Set(evidence.data?.results.map((e) => e.snapshot)),
                  ).map((id) => ({ value: id, label: `指标快照 ${id}` }))}
                />
              </Form.Item>
              <Form.Item name="risk_resolved" valuePropName="checked">
                <Checkbox>已核查并确认风险解除（“已解决”必选）</Checkbox>
              </Form.Item>
            </>
          )}
          {operation === "snooze" && (
            <Form.Item
              name="snoozed_until"
              label="暂缓截止"
              rules={[{ required: true }]}
            >
              <Input type="datetime-local" />
            </Form.Item>
          )}
          <Form.Item
            name="reason"
            label="处理依据"
            rules={[{ required: true }]}
          >
            <Input.TextArea rows={2} />
          </Form.Item>
          <Button htmlType="submit" type="primary" loading={change.isPending}>
            提交处理
          </Button>
        </Form>
      )}
      <Typography.Title level={5}>关联动作</Typography.Title>
      <Space orientation="vertical">
        {tasks.data?.results.map((t) => (
          <span key={t.id}>
            {t.title} · {t.status === "completed" ? "已完成" : "待执行"}
          </span>
        ))}
      </Space>
      {create.error && <Alert type="error" title={create.error.message} />}
      <Form
        name={`event-task-${eventId}`}
        layout="inline"
        onFinish={(values) => create.mutate(values)}
        style={{ marginTop: 12 }}
      >
        <Form.Item name="title" label="核查任务" rules={[{ required: true }]}>
          <Input />
        </Form.Item>
        <Button htmlType="submit" loading={create.isPending}>
          建立关联任务
        </Button>
      </Form>
      {create.isSuccess && (
        <p>已建立，请到“动作与复查”记录执行、生效时间和观察指标。</p>
      )}
    </section>
  );
}
