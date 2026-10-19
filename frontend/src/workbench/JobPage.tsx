import { Alert, Button, Empty, Space, Table, Tag, Typography } from "antd";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { request, type Job, type Shop } from "./api";
import useServerPage from "./useServerPage";
import ListError from "./ListError";
import { jobStatusLabel, jobResultSummary } from "./executionStatus";
export default function JobPage({
  shop,
  canEdit,
}: {
  shop: Shop;
  canEdit: boolean;
}) {
  const cache = useQueryClient();
  const jobs = useServerPage<Job>({
    key: "jobs",
    scope: shop.id,
    path: `jobs/?shop=${shop.id}`,
    pollActiveJobs: true,
  });
  const probe = useMutation({
    mutationFn: () =>
      request("jobs/probe/", "POST", {
        shop: shop.id,
        idempotency_key: crypto.randomUUID(),
      }),
    onSuccess: () => {
      jobs.setPage(1);
      void cache.invalidateQueries({ queryKey: ["jobs", shop.id] });
    },
  });
  return (
    <>
      <div className="wb-page-heading">
        <div>
          <Typography.Title level={2}>后台任务</Typography.Title>
          <p>查看当前店铺的执行记录。执行结束后仍需核查调查证据与限制。</p>
        </div>
        <Space wrap>
          <Button onClick={() => void jobs.refetch()} loading={jobs.isFetching}>
            刷新任务列表
          </Button>
          {canEdit && (
            <Button onClick={() => probe.mutate()} loading={probe.isPending}>
              检查后台服务
            </Button>
          )}
        </Space>
      </div>
      {probe.error && (
        <Alert type="error" showIcon title={probe.error.message} />
      )}
      <ListError
        title="任务列表读取失败"
        error={jobs.error}
        retry={() => jobs.refetch()}
      />
      <Table<Job>
        rowKey="id"
        dataSource={jobs.data?.results ?? []}
        loading={jobs.isLoading}
        scroll={{ x: 800 }}
        pagination={jobs.pagination}
        locale={{
          emptyText: (
            <Empty
              description={jobs.error ? "任务列表读取失败" : "暂无后台任务"}
            />
          ),
        }}
        columns={[
          {
            title: "任务",
            render: (_, j) =>
              (
                ({
                  probe: "后台服务检查",
                  investigation_run: "自主调查",
                  semantic_run: "语义取证",
                }) as Record<string, string>
              )[j.kind] ?? j.kind,
          },
          {
            title: "状态",
            render: (_, j) => (
              <Tag
                color={
                  j.status === "failed"
                    ? "red"
                    : j.status === "retry_wait"
                      ? "gold"
                      : "default"
                }
              >
                {jobStatusLabel(j.status)}
              </Tag>
            ),
          },
          {
            title: "发起时间",
            render: (_, j) => new Date(j.created_at).toLocaleString("zh-CN"),
          },
          { title: "结果", render: (_, j) => jobResultSummary(j) },
        ]}
      />
    </>
  );
}
