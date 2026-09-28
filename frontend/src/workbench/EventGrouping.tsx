import { Alert, Button, Form, Input, Select, Table, Typography } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Page } from "./api";
import type { components } from "./schema";
type Event = components["schemas"]["Event"];
export default function EventGrouping({ event }: { event: Event }) {
  const cache = useQueryClient();
  const [form] = Form.useForm();
  const related = useQuery({
    queryKey: ["related-events", event.id],
    queryFn: async () => {
      const rows: Event[] = [];
      for (let page = 1; ; page++) {
        const data = await request<Page<Event>>(
          `events/${event.id}/related/?page=${page}`,
        );
        rows.push(...data.results);
        if (!data.next) return rows;
      }
    },
  });
  const rows = [event, ...(related.data ?? [])];
  const command = useMutation({
    mutationFn: (values: {
      operation: string;
      selected: string[];
      title?: string;
      reason: string;
    }) =>
      request(`events/${event.id}/group-command/`, "POST", {
        operation: values.operation,
        title: values.title,
        reason: values.reason,
        events: Object.fromEntries(
          rows
            .filter((row) => [event.id, ...values.selected].includes(row.id))
            .map((row) => [row.id, row.version]),
        ),
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      form.resetFields();
      void cache.invalidateQueries({ queryKey: ["events"] });
      void cache.invalidateQueries({ queryKey: ["event"] });
      void cache.invalidateQueries({ queryKey: ["related-events"] });
    },
  });
  return (
    <section>
      <Typography.Title level={4}>关联事件与问题组</Typography.Title>
      <Alert
        type="info"
        title={
          event.group_title
            ? `当前问题组：${event.group_title}`
            : "同一商品的其他异常"
        }
        description="不同问题族仅作关联提示，不代表同一原因。合并后保留各成员的证据、紧急度和处理状态；安全事件仍独立提醒，关闭一个成员不会关闭整个组。"
      />
      {(related.error || command.error) && (
        <Alert type="error" title={(related.error || command.error)!.message} />
      )}
      <Table
        rowKey="id"
        dataSource={rows}
        pagination={{ pageSize: 5 }}
        columns={[
          { title: "事件", dataIndex: "title" },
          { title: "问题族", dataIndex: "family" },
          {
            title: "问题组",
            render: (_, row: Event) => row.group_title || "未分组",
          },
          {
            title: "紧急度",
            render: (_, row: Event) =>
              ["紧急", "高", "普通", "观察"][row.priority],
          },
        ]}
      />
      <Form
        form={form}
        name={`group-${event.id}`}
        layout="vertical"
        onFinish={(values) => command.mutate(values)}
        disabled={command.isPending}
      >
        <Form.Item
          name="operation"
          label="分组操作"
          rules={[{ required: true }]}
        >
          <Select
            options={[
              { value: "merge", label: "合并为问题组" },
              { value: "split", label: "从原问题组拆分" },
            ]}
          />
        </Form.Item>
        <Form.Item
          name="selected"
          label="其他事件（当前事件始终选中）"
          initialValue={[]}
        >
          <Select
            mode="multiple"
            options={(related.data ?? []).map((row) => ({
              value: row.id,
              label: `${row.title} · ${row.family} · ${row.id.slice(0, 8)}`,
            }))}
          />
        </Form.Item>
        <Form.Item name="title" label="问题组名称（合并时必填）">
          <Input maxLength={240} />
        </Form.Item>
        <Form.Item
          name="reason"
          label="合并或拆分依据"
          rules={[{ required: true, min: 3 }]}
        >
          <Input.TextArea maxLength={1000} />
        </Form.Item>
        <Button htmlType="submit" loading={command.isPending}>
          保存问题组调整
        </Button>
      </Form>
    </section>
  );
}
