import ReactECharts from "echarts-for-react";
import type { Topic, TrendPoint } from "../lib/api";

const axis = {
  axisLine: { show: false },
  axisTick: { show: false },
  axisLabel: { color: "#64748b", fontSize: 11 },
  splitLine: { lineStyle: { color: "rgba(15, 23, 42, .06)", type: "dashed" as const } },
};

export function TrendChart({ points, compact = false }: { points: TrendPoint[]; compact?: boolean }) {
  const anomalies = points.map((point) => (point.is_injected ? point.orders : "-"));
  const option = {
    animationDuration: 900,
    animationEasing: "cubicOut",
    color: ["#4facfe", "#0891b2", "#b91c1c"],
    grid: { left: 42, right: 50, top: compact ? 22 : 38, bottom: 34 },
    tooltip: {
      trigger: "axis",
      backgroundColor: "rgba(17, 25, 48, .94)",
      borderWidth: 0,
      textStyle: { color: "#fff", fontSize: 12 },
      padding: [10, 12],
      axisPointer: { lineStyle: { color: "rgba(79,172,254,.35)" } },
    },
    legend: compact ? undefined : { top: 0, right: 6, itemWidth: 18, itemHeight: 3, textStyle: { color: "#475569" } },
    xAxis: { ...axis, type: "category", boundaryGap: false, data: points.map((point) => point.month.slice(2)) },
    yAxis: [
      { ...axis, type: "value", name: "订单量", nameTextStyle: { color: "#9ba3b3", fontSize: 10 } },
      { ...axis, type: "value", name: "退款率 %", nameTextStyle: { color: "#9ba3b3", fontSize: 10 }, splitLine: { show: false } },
    ],
    series: [
      {
        name: "订单量",
        type: "line",
        smooth: 0.35,
        symbol: "circle",
        symbolSize: 6,
        showSymbol: false,
        lineStyle: { width: 3, color: "#4facfe" },
        areaStyle: {
          color: {
            type: "linear",
            x: 0,
            y: 0,
            x2: 0,
            y2: 1,
            colorStops: [
              { offset: 0, color: "rgba(79, 172, 254, .22)" },
              { offset: 1, color: "rgba(79, 172, 254, 0)" },
            ],
          },
        },
        data: points.map((point) => point.orders),
      },
      {
        name: "退款率",
        type: "line",
        yAxisIndex: 1,
        smooth: 0.3,
        showSymbol: false,
        lineStyle: { width: 2.4, color: "#0891b2" },
        data: points.map((point) => point.refund_rate),
      },
      {
        name: "异常注入点",
        type: "scatter",
        data: anomalies,
        symbolSize: 14,
        itemStyle: { color: "#b91c1c", borderColor: "#fff", borderWidth: 4, shadowBlur: 18, shadowColor: "rgba(185,28,28,.35)" },
      },
    ],
  };
  return <ReactECharts option={option} style={{ height: compact ? 240 : 310 }} opts={{ renderer: "svg" }} />;
}

export function TopicChart({ topics }: { topics: Topic[] }) {
  const option = {
    animationDuration: 850,
    color: ["#047857", "#94a3b8", "#b91c1c"],
    grid: { left: 88, right: 16, top: 12, bottom: 24 },
    tooltip: { trigger: "axis", axisPointer: { type: "shadow" }, backgroundColor: "rgba(17,25,48,.94)", borderWidth: 0, textStyle: { color: "#fff" } },
    legend: { bottom: 0, itemWidth: 9, itemHeight: 9, textStyle: { color: "#64748b", fontSize: 10 } },
    xAxis: { ...axis, type: "value", splitLine: { lineStyle: { color: "rgba(15,23,42,.06)" } } },
    yAxis: { ...axis, type: "category", inverse: true, data: topics.slice(0, 6).map((item) => item.label) },
    series: [
      { name: "正向", type: "bar", stack: "total", barWidth: 12, itemStyle: { borderRadius: [6, 0, 0, 6] }, data: topics.slice(0, 6).map((item) => item.positive) },
      { name: "中性/混合", type: "bar", stack: "total", barWidth: 12, data: topics.slice(0, 6).map((item) => item.neutral) },
      { name: "负向", type: "bar", stack: "total", barWidth: 12, itemStyle: { borderRadius: [0, 6, 6, 0] }, data: topics.slice(0, 6).map((item) => item.negative) },
    ],
  };
  return <ReactECharts option={option} style={{ height: 278 }} opts={{ renderer: "svg" }} />;
}
