export type ReviewMetricValue = string | number | null | undefined;

const percentageMetrics = new Set(["unit_session_percent", "refunds", "returns"]);

function formatDelta(value: ReviewMetricValue) {
  if (value === null || value === undefined || value === "") return "未知";
  const text = String(value);
  return text.startsWith("-") ? text : "+" + text;
}

export function displayMetricValue(metric: string, value: ReviewMetricValue) {
  if (value === null || value === undefined || value === "") return "未知";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (percentageMetrics.has(metric)) return (numeric * 100).toFixed(2) + "%";
  return Number.isInteger(numeric) ? numeric.toLocaleString("zh-CN") : numeric.toFixed(2);
}

export function displayMetricDelta(metric: string, value: ReviewMetricValue) {
  if (value === null || value === undefined || value === "") return "未知";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return String(value);
  if (percentageMetrics.has(metric)) {
    const points = numeric * 100;
    return (points > 0 ? "+" : "") + points.toFixed(2) + " 个百分点";
  }
  return formatDelta(value) + (metric === "ordered_units" ? " 件／日" : metric === "sessions" ? " 次／日" : "");
}
