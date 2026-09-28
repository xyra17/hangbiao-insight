import { useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  Space,
  Switch,
  Table,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request, type Page, type Shop } from "./api";
import type { components } from "./schema";
type Schedule = components["schemas"]["Schedule"];
type Notice = components["schemas"]["Notification"];
export default function ScanControls({
  shop,
  canEdit,
  onEvent,
}: {
  shop: Shop;
  canEdit: boolean;
  onEvent: (id: string) => void;
}) {
  const cache = useQueryClient();
  const [openSchedule, setOpenSchedule] = useState(false);
  const [openNotices, setOpenNotices] = useState(false);
  const [page, setPage] = useState(1);
  const [form] = Form.useForm();
  const schedules = useQuery({
    queryKey: ["scan-schedule", shop.id],
    queryFn: () => request<Page<Schedule>>(`scan-schedules/?shop=${shop.id}`),
  });
  const schedule = schedules.data?.results[0];
  const notices = useQuery({
    queryKey: ["notifications", shop.id, page],
    queryFn: () =>
      request<Page<Notice>>(
        `notifications/?shop=${shop.id}&unread=1&page=${page}`,
      ),
    refetchInterval: 5000,
  });
  const save = useMutation({
    mutationFn: (values: Record<string, unknown>) =>
      request<Schedule>("scan-schedules/", "POST", {
        ...values,
        shop: shop.id,
      }),
    onSuccess: () => {
      setOpenSchedule(false);
      void cache.invalidateQueries({ queryKey: ["scan-schedule", shop.id] });
    },
  });
  const read = useMutation({
    mutationFn: (id: string) =>
      request<Notice>(`notifications/${id}/read/`, "POST"),
    onSuccess: () => {
      setPage(1);
      void cache.invalidateQueries({ queryKey: ["notifications", shop.id] });
    },
  });
  const labels: Record<string, string> = {
    new: "新事件",
    worsened: "明显恶化",
    reopened: "重新打开",
    assigned: "已分派",
    review_overdue: "复查逾期",
  };
  return (
    <>
      <Space wrap style={{ marginTop: 8 }}>
        <Typography.Text type="secondary">
          {schedule
            ? `每日扫描：${schedule.enabled ? schedule.at.slice(0, 5) : "已停用"}（${schedule.timezone}）`
            : "首次发布主来源报表后启用每日扫描"}
        </Typography.Text>
        {canEdit && (
          <Button
            disabled={schedules.isLoading || !!schedules.error}
            onClick={() => {
              form.setFieldsValue({
                at: schedule?.at ?? "08:00:00",
                enabled: schedule?.enabled ?? true,
                expected_version: schedule?.version ?? 0,
              });
              save.reset();
              setOpenSchedule(true);
            }}
          >
            设置扫描时间
          </Button>
        )}
        <Button onClick={() => setOpenNotices(true)}>
          待办提醒（{notices.data?.count ?? 0}）
        </Button>
      </Space>
      {(schedules.error || notices.error || schedule?.error) && (
        <Alert
          type="warning"
          title={
            schedules.error?.message ||
            notices.error?.message ||
            schedule?.error
          }
        />
      )}
      <Drawer
        title="每日扫描计划"
        open={openSchedule}
        onClose={() => setOpenSchedule(false)}
      >
        <p>扫描时间按工作区时区执行；指标窗口始终按店铺的站点日期计算。</p>
        <p>新主来源报表发布后也会触发扫描，重复扫描不会重复建立事件。</p>
        {save.error && (
          <Alert
            type="error"
            title={save.error.message}
            action={
              <Button
                onClick={async () => {
                  const latest = await schedules.refetch();
                  const value = latest.data?.results[0];
                  if (value)
                    form.setFieldsValue({
                      ...value,
                      expected_version: value.version,
                    });
                }}
              >
                读取最新配置
              </Button>
            }
          />
        )}
        <Form
          form={form}
          layout="vertical"
          onFinish={(values) => save.mutate(values)}
        >
          <Form.Item name="expected_version" hidden>
            <Input />
          </Form.Item>
          <Form.Item
            name="at"
            label="每日扫描时间"
            rules={[{ required: true }]}
          >
            <Input type="time" step={60} />
          </Form.Item>
          <Form.Item
            name="enabled"
            label="启用每日扫描"
            valuePropName="checked"
          >
            <Switch />
          </Form.Item>
          <Button type="primary" htmlType="submit" loading={save.isPending}>
            保存扫描计划
          </Button>
        </Form>
      </Drawer>
      <Drawer
        title="待办提醒"
        open={openNotices}
        onClose={() => setOpenNotices(false)}
        size="large"
      >
        {read.error && <Alert type="error" title={read.error.message} />}
        <Table<Notice>
          rowKey="id"
          dataSource={notices.data?.results}
          pagination={{
            current: page,
            pageSize: 50,
            total: notices.data?.count,
            onChange: setPage,
            showSizeChanger: false,
          }}
          columns={[
            { title: "事项", dataIndex: "event_title" },
            { title: "变化", render: (_, n) => labels[n.kind] ?? n.kind },
            {
              title: "操作",
              render: (_, n) => (
                <Space>
                  <Button
                    type="link"
                    onClick={() => {
                      setOpenNotices(false);
                      if (n.event) onEvent(n.event);
                      else window.location.assign("/actions");
                    }}
                  >
                    查看依据
                  </Button>
                  <Button
                    loading={read.isPending}
                    onClick={() => read.mutate(n.id)}
                  >
                    标记已读
                  </Button>
                </Space>
              ),
            },
          ]}
        />
      </Drawer>
    </>
  );
}
