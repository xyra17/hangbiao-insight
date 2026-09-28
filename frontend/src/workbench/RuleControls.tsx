import { useEffect, useState } from "react";
import {
  Alert,
  Button,
  Drawer,
  Form,
  Input,
  InputNumber,
  Space,
  Table,
  Typography,
} from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { request } from "./api";
import type { components } from "./schema";
type Result = components["schemas"]["RuleResult"];
type Values = components["schemas"]["RuleSettings"];
type CatalogItem = {
  label: string;
  min: number;
  max: number;
  integer: boolean;
};
type Signal = { metric: string; title: string; priority: number };
export default function RuleControls({
  shopId,
  asin,
  productId,
}: {
  shopId: string;
  asin?: string;
  productId?: string;
}) {
  const [open, setOpen] = useState(false);
  const [form] = Form.useForm();
  const [reason, setReason] = useState("");
  const [preview, setPreview] = useState<Result>();
  const [previewJobId, setPreviewJobId] = useState<string>();
  const [previewError, setPreviewError] = useState<string>();
  const cache = useQueryClient();
  const settings = useQuery({
    queryKey: ["rule-settings", shopId],
    queryFn: () => request<Result>(`rule-configurations/${shopId}/`),
  });
  const prepare = useMutation({
    mutationFn: (values: Values) =>
      request<Result>("rule-configurations/preview/", "POST", {
        shop: shopId,
        child_asin: asin,
        product: productId,
        expected_version: settings.data?.version,
        values,
      }),
    onSuccess: (result) => {
      setPreview(undefined);
      setPreviewError(undefined);
      setPreviewJobId(result.job_id);
    },
  });
  const previewJob = useQuery({
    queryKey: ["rule-preview-job", previewJobId],
    queryFn: () => request<components["schemas"]["Job"]>(`jobs/${previewJobId}/`),
    enabled: !!previewJobId,
    refetchInterval: (query) => ["completed", "failed", "cancelled"].includes(query.state.data?.status ?? "") ? false : 1000,
  });
  useEffect(() => {
    if (previewJob.error) {
      setPreviewError(previewJob.error.message);
      setPreviewJobId(undefined);
      return;
    }
    const job = previewJob.data;
    if (!job) return;
    if (job.status === "completed") {
      setPreview(job.result as Result);
      setPreviewJobId(undefined);
    } else if (["failed", "cancelled"].includes(job.status)) {
      setPreviewError(job.error || "全店预览未完成，请重新预览。");
      setPreviewJobId(undefined);
    }
  }, [previewJob.data, previewJob.error]);
  const save = useMutation({
    mutationFn: () =>
      request<Result>("rule-configurations/apply/", "POST", {
        shop: shopId,
        preview_token: preview?.preview_token,
        reason,
      }),
    onSuccess: () => {
      setOpen(false);
      setPreview(undefined);
      void cache.invalidateQueries({ queryKey: ["rule-settings", shopId] });
      void cache.invalidateQueries({ queryKey: ["events"] });
    },
  });
  const busy = prepare.isPending || save.isPending || !!previewJobId;
  const error = settings.error || prepare.error || save.error || previewJob.error;
  return (
    <>
      <Button
        disabled={!asin || !settings.data}
        onClick={() => {
          form.setFieldsValue(settings.data?.values);
          setReason("");
          setPreview(undefined);
          prepare.reset();
          save.reset();
          setOpen(true);
        }}
      >
        调整检测阈值
      </Button>
      <Drawer
        title="检测阈值与影响预览"
        open={open}
        size="large"
        onClose={() => {
          if (!busy) setOpen(false);
        }}
      >
        <Alert
          type="info"
          title={`当前配置版本 ${settings.data?.version ?? 0}；预览对象 ${asin ?? "未选择"}${productId ? " / SKU" : " / ASIN"}`}
          description="先在后台核算全店 ASIN 和 SKU 的影响，再保存本店阈值。商品、来源数据或配置变化后须重新预览。"
        />
        {error && <Alert type="error" title={error.message} />}
        {previewError && <Alert type="error" title={previewError} />}
        {previewJobId && <Alert type="info" title="正在核算全店影响，请稍候" />}
        <Form
          form={form}
          layout="vertical"
          disabled={busy}
          onValuesChange={() => setPreview(undefined)}
          onFinish={(values: Values) => prepare.mutate(values)}
        >
          {Object.entries(settings.data?.catalog ?? {}).map(([key, value]) => {
            const item = value as CatalogItem;
            return (
              <Form.Item
                key={key}
                name={key}
                label={item.label}
                rules={[{ required: true }]}
              >
                <InputNumber
                  min={item.min}
                  max={item.max}
                  precision={item.integer ? 0 : 2}
                  style={{ width: "100%" }}
                />
              </Form.Item>
            );
          })}
          <Button htmlType="submit" loading={prepare.isPending || !!previewJobId}>
            预览全店影响
          </Button>
        </Form>
        {preview && (
          <>
            <Typography.Title level={4}>命中信号变化</Typography.Title>
            <p>{preview.scope_note}</p>
            {[
              { label: "修改前", rows: preview.before },
              { label: "修改后", rows: preview.after },
            ].map((group) => (
              <section key={group.label}>
                <Typography.Title level={5}>{group.label}</Typography.Title>
                <Table<Signal>
                  rowKey="metric"
                  pagination={false}
                  dataSource={(group.rows ?? []) as Signal[]}
                  columns={[
                    { title: "信号", dataIndex: "title" },
                    {
                      title: "紧急度",
                      render: (_, signal) =>
                        ["紧急", "高", "普通", "观察"][signal.priority],
                    },
                  ]}
                  locale={{ emptyText: "无命中信号；请同时核对数据完整性" }}
                />
              </section>
            ))}
            <Typography.Title level={4}>全店影响明细</Typography.Title>
            <Table<{child_asin: string; product: string | null; seller_sku?: string; fulfillment?: string; before: Signal[]; after: Signal[]}>
              rowKey={(row) => `${row.child_asin}:${row.product ?? "aggregate"}`}
              dataSource={(preview.impact ?? []) as {child_asin: string; product: string | null; seller_sku?: string; fulfillment?: string; before: Signal[]; after: Signal[]}[]}
              pagination={{ pageSize: 10 }}
              columns={[
                { title: "ASIN", dataIndex: "child_asin" },
                { title: "范围", render: (_, row) => row.product ? `${row.seller_sku} / ${row.fulfillment}` : "ASIN 汇总" },
                { title: "修改前", render: (_, row) => row.before.map((signal) => signal.title).join("；") || "无命中" },
                { title: "修改后", render: (_, row) => row.after.map((signal) => signal.title).join("；") || "无命中" },
              ]}
            />
            <Space
              orientation="vertical"
              style={{ width: "100%", marginTop: 16 }}
            >
              <Input.TextArea
                aria-label="阈值修改原因"
                placeholder="填写修改原因（至少 3 个字）"
                value={reason}
                maxLength={500}
                onChange={(event) => setReason(event.target.value)}
                disabled={busy}
              />
              <Button
                type="primary"
                disabled={reason.trim().length < 3}
                loading={save.isPending}
                onClick={() => save.mutate()}
              >
                保存本店阈值并重扫
              </Button>
            </Space>
          </>
        )}
        <Typography.Title level={4}>最近 20 次修改</Typography.Title>
        <Table
          rowKey="version"
          pagination={false}
          dataSource={
            (settings.data?.history ?? []) as {
              version: number;
              reason: string;
              created_at: string;
            }[]
          }
          columns={[
            { title: "版本", dataIndex: "version" },
            { title: "原因", dataIndex: "reason" },
            { title: "时间", dataIndex: "created_at" },
          ]}
        />
      </Drawer>
    </>
  );
}
