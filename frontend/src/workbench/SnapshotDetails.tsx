import { Alert, Button, Descriptions, Space, Typography } from "antd";

type AbsoluteValue = {
  status: string;
  date?: string;
  available?: number | null;
  total?: number | null;
  listing_status?: string;
  rating?: string;
  limitation?: string;
  raw_row_id?: string;
};
type OccurrenceWindow = {
  start: string;
  end: string;
  event_count: number;
  reversal_count?: number;
  amount_by_currency?: Record<string, string>;
  units?: number;
  event_basis?: string;
  raw_row_ids: string[];
};
type Occurrence = {
  status: string;
  reason?: string;
  limitation?: string;
  baseline?: OccurrenceWindow;
  current?: OccurrenceWindow;
};
const labels: Record<string, string> = {
  ready: "范围完整",
  missing: "缺少数据",
  stale: "数据过期",
  partial: "部分数据",
  invalid: "口径不一致",
};
const show = (v: string | number | null | undefined) => v ?? "未知";
export default function SnapshotDetails({
  payload,
  onTrace,
}: {
  payload: Record<string, unknown>;
  onTrace: (id: string) => void;
}) {
  const absolute = payload.absolute as
    Record<string, AbsoluteValue> | undefined;
  const occurrence = payload.occurrence as
    Record<string, Occurrence> | undefined;
  const sampledReviews = (payload.absolute as {
    sampled_reviews?: { start: string; end: string; count: number; mean_rating: string | null; limitation: string; raw_row_ids: string[] };
  } | undefined)?.sampled_reviews;
  const context = payload.comparability as
    | {
        status: string;
        limitations: string[];
        operations: {
          type: string;
          description: string;
          start: string;
          end: string | null;
          raw_row_id: string;
          windows: string[];
        }[];
        structure_changes: {
          parent_asins: string[];
          source_kind: string;
          product_id: string;
        }[];
      }
    | undefined;
  return (
    <>
      {context && (
        <>
          <Typography.Title level={4}>比较窗口内的运营变化</Typography.Title>
          <Alert
            type="warning"
            title={
              context.status === "limited"
                ? "基线可比性受限"
                : "运营背景仍需核实"
            }
            description={context.limitations.join(" ")}
          />
          {context.operations.map((item) => (
            <section key={item.raw_row_id}>
              <p>
                {item.start} 至 {item.end ?? "结束日期未知"} ·{" "}
                {item.description}（{item.type}）
              </p>
              <p>重叠窗口：{item.windows.join("、")}</p>
              <Button onClick={() => onTrace(item.raw_row_id)}>
                查看运营事件原始行
              </Button>
            </section>
          ))}
          {context.structure_changes.map((item) => (
            <p key={`${item.source_kind}:${item.product_id}`}>
              历史父体记录发生变化：
              {item.parent_asins
                .map((parent) => parent || "未关联父体")
                .join("、")}
              。需核查变体重组及子体构成，不能按当前映射覆盖历史。
            </p>
          ))}
        </>
      )}
      <Typography.Title level={4}>库存、可售与页面星级</Typography.Title>
      {(["inventory", "ratings"] as const).map((kind) => {
        const item = absolute?.[kind];
        if (!item) return null;
        return (
          <section key={kind}>
            <Typography.Title level={5}>
              {kind === "inventory" ? "可售情况" : "页面星级"}
            </Typography.Title>
            <Alert
              type={item.status === "ready" ? "info" : "warning"}
              title={labels[item.status] ?? item.status}
              description={item.limitation}
            />
            {item.date && (
              <Descriptions
                column={1}
                items={[
                  { key: "date", label: "快照日期", children: item.date },
                  ...(kind === "inventory"
                    ? [
                        {
                          key: "available",
                          label: "可售库存",
                          children: show(item.available),
                        },
                        {
                          key: "total",
                          label: "总库存",
                          children: show(item.total),
                        },
                        {
                          key: "listing",
                          label: "可售状态",
                          children: item.listing_status || "未知",
                        },
                      ]
                    : [
                        {
                          key: "rating",
                          label: "页面星级",
                          children: show(item.rating),
                        },
                      ]),
                ]}
              />
            )}
            {item.raw_row_id && (
              <Button onClick={() => onTrace(item.raw_row_id!)}>
                查看快照原始行
              </Button>
            )}
          </section>
        );
      })}
      {sampledReviews && (
        <section>
          <Typography.Title level={5}>实际采集评论均分</Typography.Title>
          <Alert type="info" title="采集样本口径" description={sampledReviews.limitation} />
          <p>{sampledReviews.start} 至 {sampledReviews.end}：{sampledReviews.count} 条；均分：
            {sampledReviews.mean_rating === null ? "暂无样本" : Number(sampledReviews.mean_rating).toFixed(2)}
          </p>
          <Space wrap>
            {sampledReviews.raw_row_ids.slice(0, 20).map((id, index) => (
              <Button key={id} onClick={() => onTrace(id)}>评论原始行 {index + 1}</Button>
            ))}
          </Space>
          {sampledReviews.count > 20 && <p className="wb-secondary">共 {sampledReviews.count} 条，当前展示前 20 条引用。</p>}
        </section>
      )}
      <Typography.Title level={4}>退款与退货发生量</Typography.Title>
      <p className="wb-secondary">
        按发生日期比较数量。退款冲正单独列出，金额按币种分别显示；不除以当期销量，不解释为缺陷率。
      </p>
      {(["refunds", "returns"] as const).map((kind) => {
        const item = occurrence?.[kind];
        if (!item) return null;
        return (
          <section key={kind}>
            <Typography.Title level={5}>
              {kind === "refunds" ? "退款发生量" : "退货发生量"}
            </Typography.Title>
            <Alert
              type={item.status === "ready" ? "info" : "warning"}
              title={labels[item.status] ?? item.status}
              description={item.reason}
            />
            {[item.baseline, item.current].map(
              (window, i) =>
                window && (
                  <div key={i}>
                    <p>
                      {i === 0 ? "基线" : "当前"}（{window.start} 至{" "}
                      {window.end}）：{window.event_count} 条
                      {kind === "refunds" ? "正向退款交易" : "退货记录"}。
                    </p>
                    {kind === "refunds" ? (
                      <p>
                        冲正：{window.reversal_count} 条；净金额：
                        {Object.entries(window.amount_by_currency ?? {})
                          .map(([currency, amount]) => `${currency} ${amount}`)
                          .join("；") || "无交易"}
                        。
                      </p>
                    ) : (
                      <p>
                        退货件数：{show(window.units)}；口径：
                        {window.event_basis === "requested"
                          ? "申请退货"
                          : "仓库收到"}
                        。
                      </p>
                    )}
                    <Space wrap>
                      {window.raw_row_ids.slice(0, 20).map((id, index) => (
                        <Button key={id} onClick={() => onTrace(id)}>
                          原始行 {index + 1}
                        </Button>
                      ))}
                    </Space>
                    {window.raw_row_ids.length > 20 && (
                      <p className="wb-secondary">
                        共 {window.raw_row_ids.length} 条，当前展示前 20
                        条引用。
                      </p>
                    )}
                  </div>
                ),
            )}
          </section>
        );
      })}
    </>
  );
}
