export const isActiveJob = (status?: string) =>
  ["queued", "running", "retry_wait"].includes(status ?? "");

export function jobStatusLabel(status: string) {
  const labels: Record<string, string> = {
    queued: "排队中",
    running: "执行中",
    retry_wait: "等待重试",
    completed: "执行结束",
    failed: "执行失败",
    cancelled: "已取消",
  };
  return labels[status] ?? "状态未知";
}

export function investigationStatus(
  status: string,
  completionStatus?: string,
): {
  label: string;
  color: string;
  type: "info" | "warning" | "error";
  description: string;
} {
  if (
    status === "completed" &&
    ["partial", "blocked"].includes(completionStatus ?? "")
  ) {
    return {
      label:
        completionStatus === "partial"
          ? "部分完成（执行已结束）"
          : "核查受阻（执行已结束）",
      color: "orange",
      type: "warning",
      description:
        "任务执行已结束，但核查仍有缺口；请查看已有证据与限制，结论尚未确认。",
    };
  }
  switch (status) {
    case "completed":
      return {
        label: "正常完成",
        color: "blue",
        type: "info",
        description: "调查执行已结束；结论是否成立仍须核查证据与限制。",
      };
    case "partial":
      return {
        label: "部分完成",
        color: "orange",
        type: "warning",
        description:
          "调查部分完成；请查看停止原因和已有证据，核查未解决事项。接续仍受剩余预算与授权限制。",
      };
    case "failed":
      return {
        label: "执行失败",
        color: "red",
        type: "error",
        description:
          "调查执行失败；请查看错误信息并联系管理员核查配置或授权，再决定是否重新发起。",
      };
    case "cancelled":
      return {
        label: "已取消",
        color: "default",
        type: "info",
        description: "调查已取消；已有证据不代表核查完整，接续不会重置预算。",
      };
    case "needs_clarification":
      return {
        label: "待澄清",
        color: "orange",
        type: "warning",
        description: "请补充调查对象或日期范围，确认后继续。",
      };
    case "queued":
      return {
        label: "排队中",
        color: "blue",
        type: "info",
        description: "调查等待执行。",
      };
    case "running":
      return {
        label: "执行中",
        color: "blue",
        type: "info",
        description: "调查正在执行，请等待状态更新。",
      };
    default:
      return {
        label: "状态未知",
        color: "default",
        type: "warning",
        description: "当前接口返回了未识别状态，请重新读取状态或联系管理员。",
      };
  }
}

export type ExecutionDecision = {
  execution_origin?: string;
  model_receipt?: { provider?: string; response_received?: boolean } | null;
};

// Requested providers and planner names alone do not establish model execution.
export function observedInvestigationMode(decisions?: ExecutionDecision[]) {
  if (!decisions?.length) return "未知（尚无可核实的执行记录）";
  const modes = decisions.map((decision) => {
    if (decision.execution_origin === "local_rule") return "规则调查";
    if (
      decision.execution_origin === "provider_response" &&
      decision.model_receipt?.provider === "deepseek" &&
      decision.model_receipt.response_received === true
    )
      return "DeepSeek 调查";
    return "未知";
  });
  return [...new Set(modes)].join(" / ");
}

export type BudgetEntry = {
  metadata?: {
    failure_code?: string | null;
    model_receipt?: { failure_code?: string | null };
  };
};

export function investigationFailures(entries?: BudgetEntry[]) {
  const messages: Record<string, string> = {
    output_truncated:
      "模型输出达到上限而被截断，本次判定未完成。请联系管理员核查输出额度；重新发起前须确认授权和费用。",
    empty_output_text:
      "模型返回空内容，本次判定未完成。请联系管理员核查供应商响应。",
    response_incomplete:
      "模型响应不完整，本次判定未完成。请联系管理员核查供应商响应。",
  };
  const codes = new Set(
    entries?.map(
      (entry) =>
        entry.metadata?.failure_code ??
        entry.metadata?.model_receipt?.failure_code,
    ),
  );
  return [...codes]
    .filter((code): code is string => !!code && !!messages[code])
    .map((code) => ({ code, message: messages[code] }));
}

export function jobResultSummary(job: {
  status: string;
  error?: string;
  result?: Record<string, unknown>;
}) {
  if (job.error) return job.error;
  if (typeof job.result?.message === "string" && job.result.message.trim())
    return job.result.message;
  switch (job.status) {
    case "completed":
      return "执行已结束；暂无摘要，请在调查或报告页核查证据。";
    case "failed":
      return "执行失败，暂无详细原因，请联系管理员核查。";
    case "cancelled":
      return "任务已取消，暂无执行摘要。";
    case "retry_wait":
      return "后台等待重试，请查看后续状态。";
    case "queued":
      return "任务排队中，等待执行结果。";
    case "running":
      return "任务执行中，等待结果。";
    default:
      return "状态未知，暂无执行摘要。";
  }
}
