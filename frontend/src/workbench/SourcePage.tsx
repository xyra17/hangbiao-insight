import { useState } from "react";
import {
  Alert,
  App,
  Button,
  Checkbox,
  Descriptions,
  Drawer,
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
import type { components } from "./schema";
import { request, type Job, type Page, type Shop } from "./api";

type Source = components["schemas"]["Source"];
type Batch = components["schemas"]["Batch"];
type Row = components["schemas"]["Row"];
type Catalog = components["schemas"]["SourceCatalog"];
const labels: Record<string, string> = {
  child_asin: "子 ASIN",
  business_date: "业务日期",
  seller_sku: "SKU",
  fulfillment: "履约方式",
  ordered_units: "订购件数",
  order_count: "订单数",
  sessions: "访问次数",
  buybox_percent: "购物车占比（百分数）",
  sales_amount: "销售金额",
  currency: "币种",
  record_id: "来源记录 ID",
  order_id: "订单 ID",
  order_line_id: "订单行 ID",
  transaction_chain_id: "退款交易链 ID",
  quantity: "件数",
  shipped_date: "发货日期",
  shipped_at: "发货时间（含时区）",
  occurred_at: "退款发生时间（含时区）",
  requested_at: "申请退货时间（含时区）",
  received_at: "仓库收到时间（含时区）",
  verified_purchase: "已购标记（未知留空）",
  is_vine: "Vine 标记（未知留空）",
  shipped_quantity: "已发货件数",
  original_line_id: "原订单行 ID",
  is_exchange: "是否换货",
  status: "状态",
  amount: "退款金额（冲正为负）",
  reason: "原因",
  requested_date: "退货申请日期",
  received_date: "收到退货日期",
  title: "评论标题",
  text: "反馈原文",
  rating: "评分",
  language: "语言",
  collected_at: "采集时间",
  is_deleted: "已确认删除",
  available: "可售库存",
  reserved: "预留库存",
  inbound: "在途库存",
  unsellable: "不可售库存",
  total: "总库存",
  listing_status: "商品可售状态",
  rating_count: "评分数量",
  event_type: "事件类型",
  description: "事件说明",
  ends_on: "结束日期",
  price: "价格",
  mechanism: "促销机制",
};
const states: Record<string, string> = {
  uploaded: "等待解析",
  parsing: "解析中",
  preview: "待核对发布",
  invalid: "校验未通过",
  published: "已发布",
};

export default function SourcePage({
  shop,
  canEdit,
}: {
  shop: Shop;
  canEdit: boolean;
}) {
  const cache = useQueryClient();
  const { message } = App.useApp();
  const [selected, setSelected] = useState<string>();
  const [drawer, setDrawer] = useState(false);
  const [revising, setRevising] = useState<Source | null>(null);
  const [kind, setKind] = useState("sales_traffic");
  const [batchId, setBatchId] = useState<string>();
  const [file, setFile] = useState<File | null>(null);
  const [partial, setPartial] = useState(false);
  const [confirmed, setConfirmed] = useState(false);
  const [jobId, setJobId] = useState<string>();
  const [rowPage, setRowPage] = useState(1);
  const [batchPage, setBatchPage] = useState(1);
  const [onlyErrors, setOnlyErrors] = useState(false);
  const [sourceForm] = Form.useForm();
  const [uploadForm] = Form.useForm();
  const sources = useQuery({
    queryKey: ["sources", shop.id],
    queryFn: async () => {
      const results: Source[] = [];
      let page = 1;
      for (;;) {
        const response = await request<Page<Source>>(
          `sources/?shop=${shop.id}&page=${page}`,
        );
        results.push(...response.results);
        if (!response.next) return { results };
        page += 1;
      }
    },
    enabled: canEdit,
  });
  const catalog = useQuery({
    queryKey: ["source-catalog"],
    queryFn: () => request<Catalog>("sources/catalog/"),
    enabled: canEdit,
  });
  const current =
    sources.data?.results.find((s) => s.id === selected) ??
    sources.data?.results[0];
  const objects = useQuery({
    queryKey: ["source-objects", current?.id],
    queryFn: () =>
      request<{ child_asins: string[] }>(`sources/${current?.id}/objects/`),
    enabled: !!current,
  });
  const batches = useQuery({
    queryKey: ["imports", current?.id, batchPage],
    queryFn: () =>
      request<Page<Batch>>(`imports/?source=${current?.id}&page=${batchPage}`),
    enabled: !!current,
    refetchInterval: 2000,
  });
  const detail = useQuery({
    queryKey: ["import-detail", shop.id, batchId],
    queryFn: () => request<Batch>(`imports/${batchId}/`),
    enabled: !!batchId,
    refetchInterval: 2000,
  });
  const batch = detail.data;
  const rows = useQuery({
    queryKey: ["rows", batchId, rowPage, onlyErrors, batch?.status],
    queryFn: () =>
      request<Page<Row>>(
        `imports/${batchId}/rows/?page=${rowPage}${onlyErrors ? "&errors=1" : ""}`,
      ),
    enabled: !!batch,
  });
  const job = useQuery({
    queryKey: ["publish-job", jobId],
    queryFn: () => request<Job>(`jobs/${jobId}/`),
    enabled: !!jobId,
    refetchInterval: (query) =>
      ["completed", "failed", "cancelled"].includes(
        query.state.data?.status ?? "",
      )
        ? false
        : 2000,
  });
  const fields = catalog.data?.items.find((i) => i.kind === kind)?.fields ?? [];
  const saveSource = useMutation({
    mutationFn: (data: Record<string, unknown>) => {
      const mapping = Object.fromEntries(
        Object.entries(data.column_mapping as Record<string, string>)
          .filter(([, v]) => v?.trim())
          .map(([k, v]) => [k, v.trim()]),
      );
      const ignored_columns = String(data.ignored_columns_text ?? "")
        .split(",")
        .map((s) => s.trim())
        .filter(Boolean);
      const payload = {
        ...data,
        shop: shop.id,
        kind,
        column_mapping: mapping,
        ignored_columns,
        expected_version: revising
          ? Math.max(...revising.contracts.map((c) => c.version))
          : undefined,
      };
      return request<Source>(
        revising ? `sources/${revising.id}/contracts/` : "sources/",
        "POST",
        payload,
      );
    },
    onSuccess: (source) => {
      setDrawer(false);
      cache.setQueryData<{ results: Source[] }>(
        ["sources", shop.id],
        (old) => ({
          results: [
            ...(old?.results.filter((item) => item.id !== source.id) ?? []),
            source,
          ],
        }),
      );
      setSelected(source.id);
      setBatchPage(1);
      void cache.invalidateQueries({ queryKey: ["sources", shop.id] });
      void message.success("来源契约已保存，首次发布时请核对。");
    },
  });
  const upload = useMutation({
    mutationFn: (data: Record<string, unknown>) => {
      if (!file) throw new Error("请选择 CSV 文件。");
      const form = new FormData();
      form.append("file", file);
      form.append("coverage_start", String(data.coverage_start));
      form.append("coverage_end", String(data.coverage_end));
      form.append(
        "generated_at",
        new Date(String(data.generated_at)).toISOString(),
      );
      form.append("declared_complete", String(data.declared_complete ?? false));
      (data.declared_asins as string[]).forEach((asin) =>
        form.append("declared_asins", asin),
      );
      return request<Batch>(`sources/${current?.id}/upload/`, "POST", form);
    },
    onSuccess: (b) => {
      setBatchId(b.id);
      setRowPage(1);
      setOnlyErrors(false);
      setConfirmed(false);
      setPartial(false);
      setJobId(undefined);
      void cache.invalidateQueries({ queryKey: ["imports"] });
    },
  });
  const publish = useMutation({
    mutationFn: () =>
      request<Job>(`imports/${batchId}/publish/`, "POST", {
        allow_partial: partial,
        confirm_contract: confirmed,
      }),
    onSuccess: (j) => setJobId(j.id),
  });
  function openSource(source: Source | null) {
    setRevising(source);
    saveSource.reset();
    sourceForm.resetFields();
    setDrawer(true);
    const chosenKind = source?.kind ?? "sales_traffic";
    setKind(chosenKind);
    if (source) {
      const contract = [...source.contracts].sort(
        (a, b) => b.version - a.version,
      )[0];
      sourceForm.setFieldsValue({
        ...source,
        ...contract,
        ignored_columns_text: Array.isArray(contract.ignored_columns)
          ? contract.ignored_columns.join(",")
          : "",
      });
    } else
      sourceForm.setFieldsValue({
        grain: "asin_day",
        coverage_basis: "business_date",
        return_event: "received",
        empty_report_means_zero: false,
        is_primary: true,
        column_mapping: Object.fromEntries(
          (
            catalog.data?.items.find((i) => i.kind === chosenKind)?.fields ?? []
          ).map((f) => [f.name, f.name]),
        ),
      });
  }
  function template() {
    const item = catalog.data?.items.find((i) => i.kind === current?.kind);
    if (!item) return;
    const blob = new Blob([item.fields.map((f) => f.name).join(",") + "\n"], {
      type: "text/csv;charset=utf-8",
    });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = `${current?.kind}-template.csv`;
    a.click();
    URL.revokeObjectURL(url);
  }
  if (!canEdit)
    return <Empty description="数据源由拥有整店数据管理权限的管理员维护。" />;
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>数据源</Typography.Title>
          <p>先确认报表范围和字段，再发布为可追溯的经营事实。</p>
        </div>
        <Button
          type="primary"
          disabled={!catalog.data}
          onClick={() => openSource(null)}
        >
          登记数据源
        </Button>
      </div>
      {(sources.error || catalog.error || objects.error || batches.error) && (
        <Alert
          type="error"
          title={
            (sources.error || catalog.error || objects.error || batches.error)
              ?.message
          }
        />
      )}
      <Space wrap className="wb-source-toolbar">
        <Select
          aria-label="选择数据源"
          style={{ minWidth: 260 }}
          value={current?.id}
          options={sources.data?.results.map((s) => ({
            value: s.id,
            label: s.name,
          }))}
          onChange={(id) => {
            setSelected(id);
            setBatchPage(1);
            setBatchId(undefined);
            setJobId(undefined);
            uploadForm.resetFields();
          }}
        />
        {current && (
          <>
            <Tag>{current.is_primary ? "主来源" : "补充来源"}</Tag>
            <Button onClick={() => openSource(current)}>修订字段映射</Button>
            <Button onClick={template}>下载标准列模板</Button>
          </>
        )}
      </Space>
      {current ? (
        <>
          {current.directory && (
            <Alert
              type={current.directory.error ? "warning" : "info"}
              title={
                current.directory.enabled ? "已配置目录导入" : "目录导入已停用"
              }
              description={
                current.directory.error ||
                `文件模式：${current.directory.pattern}；扫描间隔 ${current.directory.scan_interval_seconds} 秒；${current.directory.auto_publish ? "已确认契约的无错误报表自动发布" : "解析后等待人工发布"}`
              }
            />
          )}
          <section className="wb-source-upload">
            <Typography.Title level={4}>上传报表</Typography.Title>
            <p>
              模板只定义系统支持的列；原始报表可通过字段映射接入。未提供的值保留未知，完整性须由报表来源确认。
            </p>
            {upload.error && (
              <Alert type="error" title={upload.error.message} />
            )}
            <Form
              form={uploadForm}
              layout="vertical"
              onFinish={(data) => upload.mutate(data)}
            >
              <div className="wb-form-grid">
                <Form.Item label="CSV 文件" required>
                  <input
                    aria-label="CSV 文件"
                    type="file"
                    accept=".csv"
                    onChange={(event) =>
                      setFile(event.target.files?.[0] ?? null)
                    }
                  />
                </Form.Item>
                <Form.Item
                  name="generated_at"
                  label="报表生成时间（本机时区）"
                  rules={[{ required: true }]}
                >
                  <Input type="datetime-local" />
                </Form.Item>
                <Form.Item
                  name="coverage_start"
                  label="覆盖起始日"
                  rules={[{ required: true }]}
                >
                  <Input type="date" />
                </Form.Item>
                <Form.Item
                  name="coverage_end"
                  label="覆盖结束日"
                  rules={[{ required: true }]}
                >
                  <Input type="date" />
                </Form.Item>
                <Form.Item
                  name="declared_asins"
                  label="报表覆盖的子 ASIN"
                  rules={[{ required: true }]}
                >
                  <Select
                    mode="multiple"
                    loading={objects.isFetching}
                    disabled={!objects.data || objects.isFetching}
                    options={objects.data?.child_asins.map((asin) => ({
                      value: asin,
                    }))}
                  />
                </Form.Item>
                <Form.Item name="declared_complete" valuePropName="checked">
                  <Checkbox>来源确认所选对象及日期范围完整</Checkbox>
                </Form.Item>
              </div>
              <Button
                type="primary"
                htmlType="submit"
                loading={upload.isPending}
                disabled={!objects.data || objects.isFetching}
              >
                上传并解析
              </Button>
            </Form>
          </section>
          <Typography.Title level={4}>导入记录</Typography.Title>
          <Table<Batch>
            rowKey="id"
            loading={batches.isLoading}
            dataSource={batches.data?.results}
            scroll={{ x: 800 }}
            pagination={{
              current: batchPage,
              pageSize: 50,
              total: batches.data?.count,
              onChange: setBatchPage,
              showSizeChanger: false,
            }}
            columns={[
              { title: "文件", dataIndex: "filename" },
              {
                title: "覆盖窗口",
                render: (_, b) => `${b.coverage_start} — ${b.coverage_end}`,
              },
              {
                title: "状态",
                render: (_, b) => <Tag>{states[b.status] ?? b.status}</Tag>,
              },
              {
                title: "有效 / 错误行",
                render: (_, b) => `${b.valid_rows} / ${b.error_rows}`,
              },
              {
                title: "发布版本",
                render: (_, b) => b.published_revision ?? "未发布",
              },
              {
                title: "操作",
                render: (_, b) => (
                  <Button
                    type="link"
                    onClick={() => {
                      setBatchId(b.id);
                      setRowPage(1);
                      setOnlyErrors(false);
                      setConfirmed(false);
                      setPartial(false);
                      setJobId(undefined);
                      publish.reset();
                    }}
                  >
                    核对与追溯
                  </Button>
                ),
              },
            ]}
          />
        </>
      ) : (
        <Empty description="尚未登记数据源。先选择报表类型、原生粒度和字段映射。" />
      )}
      <Drawer
        title={revising ? "修订字段映射" : "登记数据源"}
        open={drawer}
        onClose={() => setDrawer(false)}
        size="large"
        destroyOnHidden
      >
        {saveSource.error && (
          <Alert type="error" title={saveSource.error.message} />
        )}
        <Form
          form={sourceForm}
          layout="vertical"
          onFinish={(data) => saveSource.mutate(data)}
        >
          {!revising && (
            <>
              <Form.Item
                name="name"
                label="来源名称"
                rules={[{ required: true }]}
              >
                <Input />
              </Form.Item>
              <Form.Item label="报表类型">
                <Select
                  value={kind}
                  options={catalog.data?.items.map((i) => ({
                    value: i.kind,
                    label: i.label,
                  }))}
                  onChange={(value) => {
                    setKind(value);
                    sourceForm.setFieldsValue({
                      coverage_basis: "business_date",
                      return_event: "received",
                      empty_report_means_zero: false,
                      grain: ["sales_traffic", "inventory", "ratings"].includes(
                        value,
                      )
                        ? "asin_day"
                        : ["orders", "refunds"].includes(value)
                          ? "product_record"
                          : "asin_record",
                    });
                    sourceForm.setFieldValue(
                      "column_mapping",
                      Object.fromEntries(
                        (
                          catalog.data?.items.find((i) => i.kind === value)
                            ?.fields ?? []
                        ).map((f) => [f.name, f.name]),
                      ),
                    );
                  }}
                />
              </Form.Item>
              <Form.Item name="is_primary" valuePropName="checked">
                <Checkbox>设为此店铺该数据域的主来源</Checkbox>
              </Form.Item>
            </>
          )}
          <Form.Item
            name="grain"
            label="报表原生粒度"
            rules={[{ required: true }]}
          >
            <Select
              options={[
                { value: "asin_day", label: "子 ASIN × 日期（不拆分 SKU）" },
                { value: "product_day", label: "SKU × 履约 × 日期" },
                { value: "asin_record", label: "子 ASIN × 来源记录" },
                { value: "product_record", label: "SKU × 履约 × 来源记录" },
              ]}
            />
          </Form.Item>
          {!["sales_traffic", "inventory", "ratings"].includes(kind) && (
            <Form.Item name="empty_report_means_zero" valuePropName="checked">
              <Checkbox>
                来源保证：范围完整的空报表代表零事件（必须有表头）
              </Checkbox>
            </Form.Item>
          )}
          {kind === "orders" && (
            <Form.Item
              name="coverage_basis"
              label="报表覆盖日期口径"
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  {
                    value: "business_date",
                    label: "下单日期（不能保证成熟发货队列分母）",
                  },
                  {
                    value: "shipped_date",
                    label: "发货日期（来源须保证完整覆盖）",
                  },
                ]}
              />
            </Form.Item>
          )}
          {kind === "returns" && (
            <Form.Item
              name="return_event"
              label="退货统计事件"
              rules={[{ required: true }]}
            >
              <Select
                options={[
                  { value: "received", label: "仓库收到退货" },
                  { value: "requested", label: "买家申请退货" },
                ]}
              />
            </Form.Item>
          )}
          <p>
            右侧填写原报表表头。非必需字段不存在时留空；不要将“订单数”映射为“件数”。
          </p>
          <div className="wb-field-mapping">
            {fields.map((f) => (
              <Form.Item
                key={`${kind}-${f.name}`}
                name={["column_mapping", f.name]}
                label={`${labels[f.name] ?? f.name}${f.required ? "（必需）" : ""}`}
                rules={[{ required: f.required, message: "请填写原报表列名" }]}
              >
                <Input placeholder="未提供" />
              </Form.Item>
            ))}
          </div>
          <Form.Item
            name="ignored_columns_text"
            label="明确不使用的其他列（逗号分隔）"
          >
            <Input />
          </Form.Item>
          <Button
            type="primary"
            htmlType="submit"
            loading={saveSource.isPending}
          >
            保存来源契约
          </Button>
        </Form>
      </Drawer>
      {detail.error && <Alert type="error" title={detail.error.message} />}
      <Drawer
        title="导入核对与追溯"
        open={!!batch}
        onClose={() => setBatchId(undefined)}
        size="large"
        destroyOnHidden
      >
        {batch && (
          <>
            <Descriptions
              column={1}
              items={[
                { key: "file", label: "文件", children: batch.filename },
                { key: "state", label: "状态", children: states[batch.status] },
                {
                  key: "coverage",
                  label: "完整性",
                  children:
                    batch.coverage_status === "ready"
                      ? "范围完整"
                      : batch.coverage_status === "partial"
                        ? "部分数据（不用于要求完整分母的检测）"
                        : "发布时校验",
                },
                {
                  key: "hash",
                  label: "文件指纹",
                  children: (
                    <Typography.Text
                      copyable
                      style={{ wordBreak: "break-all" }}
                    >
                      {batch.file_hash}
                    </Typography.Text>
                  ),
                },
                {
                  key: "download",
                  label: "原始报表",
                  children: (
                    <a href={`/api/v1/imports/${batch.id}/original-file/`}>
                      下载上传时的原文件
                    </a>
                  ),
                },
                {
                  key: "rows",
                  label: "有效 / 错误行",
                  children: `${batch.valid_rows} / ${batch.error_rows}`,
                },
                {
                  key: "scope",
                  label: "覆盖范围",
                  children: `${batch.coverage_start} 至 ${batch.coverage_end}`,
                },
                {
                  key: "version",
                  label: "发布版本",
                  children: batch.published_revision ?? "未发布",
                },
              ]}
            />
            {batch.error && <Alert type="error" title={batch.error} />}
            {batch.error_rows > 0 && (
              <Alert
                type="warning"
                title="存在错误行，完整性不能标为正常。"
                action={
                  <a href={`/api/v1/imports/${batch.id}/error-file/`}>
                    下载错误清单
                  </a>
                }
              />
            )}
            {batch.status === "preview" && (
              <section className="wb-publish">
                <Checkbox
                  checked={confirmed}
                  onChange={(e) => setConfirmed(e.target.checked)}
                >
                  已核对来源字段、粒度和覆盖范围
                </Checkbox>
                {batch.error_rows > 0 && (
                  <Checkbox
                    checked={partial}
                    onChange={(e) => setPartial(e.target.checked)}
                  >
                    确认仅发布有效行，并标记为部分数据
                  </Checkbox>
                )}
                <Button
                  type="primary"
                  onClick={() => publish.mutate()}
                  disabled={
                    !confirmed ||
                    (batch.error_rows > 0 && !partial) ||
                    ["queued", "running", "retry_wait"].includes(
                      job.data?.status ?? "",
                    )
                  }
                  loading={publish.isPending}
                >
                  确认发布
                </Button>
              </section>
            )}
            {(publish.error || job.error || job.data?.error) && (
              <Alert
                type="error"
                title={
                  publish.error?.message ||
                  job.error?.message ||
                  job.data?.error
                }
              />
            )}
            {job.data && (
              <p>
                发布任务：
                {job.data.status === "completed"
                  ? "已完成"
                  : job.data.status === "failed"
                    ? "失败"
                    : "处理中"}
              </p>
            )}
            <Checkbox
              checked={onlyErrors}
              onChange={(e) => {
                setOnlyErrors(e.target.checked);
                setRowPage(1);
              }}
            >
              只看错误行
            </Checkbox>
            {rows.error && <Alert type="error" title={rows.error.message} />}
            <Table<Row>
              rowKey="id"
              size="small"
              dataSource={rows.data?.results}
              loading={rows.isLoading}
              pagination={{
                current: rowPage,
                pageSize: 50,
                total: rows.data?.count,
                onChange: setRowPage,
                showSizeChanger: false,
              }}
              columns={[
                { title: "原文件行号", dataIndex: "line", width: 100 },
                { title: "校验", render: (_, r) => r.error || "通过" },
              ]}
              expandable={{
                expandedRowRender: (r) => (
                  <>
                    <strong>原始字段</strong>
                    <pre className="wb-raw">
                      {JSON.stringify(r.raw, null, 2)}
                    </pre>
                    <strong>规范化字段</strong>
                    <pre className="wb-raw">
                      {JSON.stringify(r.normalized, null, 2)}
                    </pre>
                  </>
                ),
              }}
            />
          </>
        )}
      </Drawer>
    </>
  );
}
