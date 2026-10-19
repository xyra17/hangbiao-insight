import { describe, expect, it } from "vitest";
import {
  investigationFailures,
  jobResultSummary,
  observedInvestigationMode,
} from "./executionStatus";
describe("observed investigation execution", () => {
  it("requires execution origin and a provider receipt before confirming DeepSeek", () => {
    expect(observedInvestigationMode()).toContain("未知");
    expect(
      observedInvestigationMode([{ execution_origin: "provider_response" }]),
    ).toBe("未知");
    expect(
      observedInvestigationMode([
        {
          execution_origin: "provider_response",
          model_receipt: { provider: "deepseek", response_received: false },
        },
      ]),
    ).toBe("未知");
    expect(
      observedInvestigationMode([
        {
          execution_origin: "provider_response",
          model_receipt: { provider: "deepseek", response_received: true },
        },
      ]),
    ).toBe("DeepSeek 调查");
    expect(
      observedInvestigationMode([{ execution_origin: "local_rule" }]),
    ).toBe("规则调查");
  });
  it("does not hide mixed or unverified execution records", () => {
    expect(
      observedInvestigationMode([
        { execution_origin: "local_rule" },
        {},
        {
          execution_origin: "provider_response",
          model_receipt: { provider: "deepseek", response_received: true },
        },
      ]),
    ).toBe("规则调查 / 未知 / DeepSeek 调查");
  });
  it("explains stable error codes only and preserves unknown causes", () => {
    expect(
      investigationFailures([
        { metadata: { failure_code: "output_truncated" } },
        { metadata: { failure_code: "output_truncated" } },
      ]),
    ).toHaveLength(1);
    expect(
      investigationFailures([
        { metadata: { model_receipt: { failure_code: "empty_output_text" } } },
      ])[0].message,
    ).toContain("空内容");
    expect(
      investigationFailures([
        { metadata: { failure_code: "response_incomplete" } },
      ])[0].message,
    ).toContain("不完整");
    expect(
      investigationFailures([
        { metadata: { failure_code: "undocumented_budget_error" } },
      ]),
    ).toEqual([]);
  });
});

describe("job summaries are based on state, not result IDs", () => {
  it("does not show waiting or quality approval for completed jobs with IDs only", () => {
    const text = jobResultSummary({
      status: "completed",
      result: { report_id: "r", investigation_id: "i" },
    });
    expect(text).toContain("执行已结束");
    expect(text).not.toContain("等待");
    expect(text).not.toContain("质量通过");
  });
  it("keeps retry, failure, cancellation and unknown summaries distinct", () => {
    expect(jobResultSummary({ status: "failed" })).toContain("执行失败");
    expect(jobResultSummary({ status: "cancelled" })).toContain("已取消");
    expect(jobResultSummary({ status: "retry_wait" })).toContain("等待重试");
    expect(jobResultSummary({ status: "new_status" })).toContain("状态未知");
  });
});
