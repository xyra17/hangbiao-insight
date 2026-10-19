import { describe, expect, it } from "vitest";
import { displayMetricDelta, displayMetricValue } from "./actionReviewFormat";

describe("action review display units", () => {
  it("formats fractional conversion ratios as percentages and percentage points", () => {
    expect(displayMetricValue("unit_session_percent", "0.1234")).toBe("12.34%");
    expect(displayMetricDelta("unit_session_percent", "0.0045")).toBe("+0.45 个百分点");
    expect(displayMetricDelta("unit_session_percent", "-0.0125")).toBe("-1.25 个百分点");
  });

  it("keeps refund and return cohort rates on the same percentage-point scale", () => {
    expect(displayMetricValue("refunds", "0.08")).toBe("8.00%");
    expect(displayMetricDelta("returns", "-0.04")).toBe("-4.00 个百分点");
  });

  it("keeps daily count changes in the correct unit", () => {
    expect(displayMetricDelta("ordered_units", "3.5")).toBe("+3.5 件／日");
    expect(displayMetricDelta("sessions", -12)).toBe("-12 次／日");
  });
});
