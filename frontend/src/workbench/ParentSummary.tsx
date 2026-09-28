import { useState } from "react";
import {
  Alert,
  Button,
  Descriptions,
  Drawer,
  Select,
  Space,
  Table,
  Typography,
} from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request, type Job, type Product, type Shop } from "./api";
import type { components } from "./schema";

type Window = {
  start: string;
  end: string;
  numerator: number;
  denominator: number;
  rate: string | null;
};
type Metric = {
  status: string;
  reason?: string;
  baseline?: Window;
  current?: Window;
};
type Cohort = { reason?: string; refunds?: Metric; returns?: Metric };
type Mix = {
  status: string;
  reason: string;
  composition_pp?: string;
  within_pp?: string;
  total_change_pp?: string;
  children: {
    child_asin: string;
    baseline_weight: string;
    current_weight: string;
  }[];
};
type Payload = {
  status: string;
  limitations: string[];
  cohort: Cohort;
  children: Record<string, Cohort>;
  decomposition: { refunds?: Mix; returns?: Mix };
};
const labels: Record<string, string> = {
  ready: "范围完整",
  partial: "数据不完整",
  missing: "缺少数据",
  stale: "数据过期",
  invalid: "口径不一致",
};
const percent = (value: string | null | undefined) =>
  value == null ? "不适用" : `${(Number(value) * 100).toFixed(2)}%`;
const count = (window?: Window) =>
  window
    ? `${window.numerator} / ${window.denominator}（${percent(window.rate)}）`
    : "缺少数据";

export default function ParentSummary({
  shop,
  products,
  onChild,
}: {
  shop: Shop;
  products: Product[];
  onChild: (asin: string) => void;
}) {
  const [open, setOpen] = useState(false);
  const [parent, setParent] = useState<string>();
  const [jobId, setJobId] = useState<string>();
  const parents = [
    ...new Set(
      products
        .flatMap((product) =>
          product.mappings.map((mapping) => mapping.parent_asin),
        )
        .filter(Boolean),
    ),
  ].sort();
  const selected = parent ?? parents[0];
  const create = useMutation({
    mutationFn: () =>
      request<Job>("parent-snapshots/", "POST", {
        shop: shop.id,
        parent_asin: selected,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: (job) => setJobId(job.id),
  });
  const job = useQuery({
    queryKey: ["parent-job", jobId],
    queryFn: () => request<Job>(`jobs/${jobId}/`),
    enabled: !!jobId,
    refetchInterval: (query) =>
      ["queued", "running", "retry_wait"].includes(
        query.state.data?.status ?? "queued",
      )
        ? 1000
        : false,
  });
  const snapshotId = job.data?.result.parent_snapshot_id as string | undefined;
  const snapshot = useQuery({
    queryKey: ["parent-snapshot", snapshotId],
    queryFn: () =>
      request<components["schemas"]["ParentSnapshot"]>(
        `parent-snapshots/${snapshotId}/`,
      ),
    enabled: !!snapshotId,
  });
  const payload = snapshot.data?.payload as Payload | undefined;
  const busy =
    create.isPending ||
    ["queued", "running", "retry_wait"].includes(job.data?.status ?? "");
  const error = create.error || job.error || snapshot.error;
  return (
    <>
      <Button disabled={!parents.length} onClick={() => setOpen(true)}>
        父体与子体对照
      </Button>
      <Drawer
        title="父体与子体对照"
        open={open}
        size="large"
        onClose={() => setOpen(false)}
      >
        <Space wrap style={{ marginBottom: 16 }}>
          <Select
            aria-label="选择父 ASIN"
            value={selected}
            style={{ width: 220 }}
            disabled={busy}
            options={parents.map((value) => ({ value, label: value }))}
            onChange={(value) => {
              setParent(value);
              setJobId(undefined);
              create.reset();
            }}
          />
          <Button
            type="primary"
            disabled={!selected || busy}
            loading={busy}
            onClick={() => create.mutate()}
          >
            计算父体汇总
          </Button>
        </Space>
        <Alert
          type="info"
          title="仅汇总本店有权限的父子商品，保留每个子体的独立事件。"
          description="订单从明细重新去重；不会把子体 Sessions 相加为父体流量。父子关系变化时不拼接趋势。"
        />
        {error && <Alert type="error" title={error.message} />}
        {job.data?.error && <Alert type="error" title={job.data.error} />}
        {payload && snapshot.data && (
          <>
            <Descriptions
              column={1}
              style={{ marginTop: 16 }}
              items={[
                {
                  key: "parent",
                  label: "父 ASIN",
                  children: snapshot.data.parent_asin,
                },
                {
                  key: "date",
                  label: "分析日期",
                  children: `${snapshot.data.local_date}（${shop.timezone}）`,
                },
                {
                  key: "version",
                  label: "计算版本",
                  children: snapshot.data.rule_version,
                },
              ]}
            />
            {payload.limitations.map((text, index) => (
              <p className="wb-secondary" key={index}>
                {text}
              </p>
            ))}
            {(["refunds", "returns"] as const).map((kind) => {
              const metric = payload.cohort[kind];
              const mix = payload.decomposition[kind];
              return (
                <section key={kind}>
                  <Typography.Title level={4}>
                    {kind === "refunds"
                      ? "成熟队列退款订单率"
                      : "成熟队列退货订单率"}
                  </Typography.Title>
                  {metric?.baseline && (
                    <p>
                      共同基线：{metric.baseline.start} 至 {metric.baseline.end}
                      ；当前：{metric.current?.start} 至 {metric.current?.end}。
                    </p>
                  )}
                  <Table
                    pagination={false}
                    rowKey="asin"
                    scroll={{ x: 600 }}
                    dataSource={[
                      ...(metric
                        ? [
                            {
                              asin: "父体整体（订单去重）",
                              metric,
                              parent: true,
                            },
                          ]
                        : []),
                      ...Object.entries(payload.children).map(
                        ([asin, child]) => ({
                          asin,
                          metric: child[kind],
                          reason: child.reason,
                          parent: false,
                        }),
                      ),
                    ]}
                    columns={[
                      {
                        title: "分析对象",
                        render: (_, row) =>
                          row.parent ? (
                            row.asin
                          ) : (
                            <Button
                              type="link"
                              onClick={() => {
                                onChild(row.asin);
                                setOpen(false);
                              }}
                            >
                              {row.asin}
                            </Button>
                          ),
                      },
                      {
                        title: "基线：相关 / 成熟订单",
                        render: (_, row) => count(row.metric?.baseline),
                      },
                      {
                        title: "当前：相关 / 成熟订单",
                        render: (_, row) => count(row.metric?.current),
                      },
                      {
                        title: "数据状态",
                        render: (_, row) => (
                          <>
                            {labels[row.metric?.status ?? "missing"]}
                            <div className="wb-secondary">
                              {row.metric?.reason}
                            </div>
                          </>
                        ),
                      },
                    ]}
                  />
                  {mix && (
                    <>
                      <Alert
                        type={mix.status === "ready" ? "info" : "warning"}
                        title={mix.reason}
                        style={{ marginTop: 12 }}
                      />
                      {mix.status === "ready" && (
                        <>
                          <p>
                            整体变化 {Number(mix.total_change_pp).toFixed(2)}{" "}
                            个百分点；订单构成变化项{" "}
                            {Number(mix.composition_pp).toFixed(2)}{" "}
                            个百分点；子体率变化项{" "}
                            {Number(mix.within_pp).toFixed(2)} 个百分点。
                          </p>
                          <Table
                            rowKey="child_asin"
                            pagination={false}
                            dataSource={mix.children}
                            columns={[
                              { title: "子 ASIN", dataIndex: "child_asin" },
                              {
                                title: "基线订单权重",
                                render: (_, row) =>
                                  percent(row.baseline_weight),
                              },
                              {
                                title: "当前订单权重",
                                render: (_, row) => percent(row.current_weight),
                              },
                            ]}
                          />
                        </>
                      )}
                    </>
                  )}
                </section>
              );
            })}
          </>
        )}
      </Drawer>
    </>
  );
}
