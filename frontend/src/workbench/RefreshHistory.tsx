import { Alert, Button, Space, Typography } from "antd";
import { useMutation, useQuery } from "@tanstack/react-query";
import { request } from "./api";
type Receipt = {
  id: string;
  status: string;
  detail: string;
  replacement_id: string | null;
  batch_id: string;
  job_id: string | null;
};
const labels: Record<string, string> = {
  queued: "等待刷新",
  running: "正在刷新",
  completed: "刷新完成",
  needs_confirmation: "需要重新确认",
  failed: "刷新失败",
  cancelled: "权限撤销或任务取消",
  retry_wait: "等待重试",
};
export default function RefreshHistory({
  kind,
  id,
  onOpen,
}: {
  kind: "reports" | "investigations";
  id: string;
  onOpen: (id: string) => void;
}) {
  const result = useQuery({
    queryKey: ["artifact-refresh", kind, id],
    queryFn: () => request<{ refreshes: Receipt[] }>(`${kind}/${id}/`),
    refetchInterval: 5000,
  });
  const retry = useMutation({
    mutationFn: (job: string) => request(`jobs/${job}/retry-refresh/`, "POST"),
    onSuccess: () => {
      void result.refetch();
    },
  });
  return (
    <Space orientation="vertical" style={{ width: "100%" }}>
      {retry.error && <Alert type="error" title={retry.error.message} />}
      {result.error && <Alert type="error" title={result.error.message} />}
      {(result.data?.refreshes ?? []).map((receipt) => (
        <Alert
          key={receipt.id}
          type={receipt.status === "completed" ? "success" : "warning"}
          title={`数据修订：${labels[receipt.status] ?? receipt.status}`}
          description={
            <>
              <Typography.Paragraph>
                {receipt.detail ||
                  "保留原版本及其动作绑定，新证据作为新版本保存。"}
              </Typography.Paragraph>
              {receipt.job_id &&
                ["failed", "cancelled"].includes(receipt.status) && (
                  <Button
                    loading={retry.isPending}
                    onClick={() => retry.mutate(receipt.job_id!)}
                  >
                    重新尝试刷新
                  </Button>
                )}
              {receipt.replacement_id && (
                <Button onClick={() => onOpen(receipt.replacement_id!)}>
                  查看刷新后的版本
                </Button>
              )}
            </>
          }
        />
      ))}
    </Space>
  );
}
