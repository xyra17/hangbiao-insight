export const API_BASE = import.meta.env.VITE_API_BASE ?? "";

export async function api<T>(path: string): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`);
  if (!response.ok) {
    throw new Error(`请求失败：${response.status}`);
  }
  return response.json() as Promise<T>;
}

export async function apiPost<T>(path: string, body: unknown): Promise<T> {
  const response = await fetch(`${API_BASE}${path}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json() as Promise<T>;
}

export async function apiUpload<T>(path: string, file: File, fields: Record<string, string> = {}): Promise<T> {
  const form = new FormData(); form.append("file", file);
  Object.entries(fields).forEach(([key, value]) => form.append(key, value));
  const response = await fetch(`${API_BASE}${path}`, { method: "POST", body: form });
  if (!response.ok) throw new Error(`请求失败：${response.status}`);
  return response.json() as Promise<T>;
}

export type Product = {
  product_id: string;
  name: string;
  category: string;
  marketplace: string;
  review_count: number;
  avg_rating: number;
  anomaly_count: number;
  status: "attention" | "healthy";
  source: string;
};

export type TrendPoint = {
  month: string;
  orders: number;
  refund_rate: number;
  rating: number | null;
  reviews: number;
  is_injected: boolean;
};

export type Anomaly = {
  id: string;
  product_id: string;
  product_name: string;
  month: string;
  severity: "high" | "medium" | "low";
  status: string;
  relation: string;
  summary: string;
  signals: Array<{ label: string; change: number; unit: string }>;
};

export type OverviewData = {
  selected_product_id: string;
  products: Product[];
  kpis: Array<{
    key: string;
    label: string;
    value: number;
    unit: string;
    delta: number;
    direction: string;
    tone: string;
  }>;
  coverage: Array<{ name: string; status: string; hint: string }>;
  trend: { product_name: string; source_version: string; points: TrendPoint[] };
  anomalies: Anomaly[];
  updated_at: string;
};

export type Topic = {
  topic: string;
  label: string;
  positive: number;
  neutral: number;
  negative: number;
  total: number;
  claim_count?: number;
  review_count?: number;
  share_delta?: number;
  insufficient_data?: boolean;
  polarity?: string;
  baseline_share?: number;
  current_share?: number;
};

export type Review = {
  review_id: string;
  product_name: string;
  rating: number;
  title: string;
  text: string;
  created_at: string;
  score: number;
  source: string;
};

export type ReviewSearchMeta = {
  retrieval_backend: string;
  retrieval_version: string;
  embedding_version: string | null;
  rerank_mode: string;
};

export type ReviewSignalCompare = {
  product_id: string;
  current_period: string;
  baseline_period: string;
  items: Array<{ secondary_topic: string; polarity: string; baseline_count: number; current_count: number; baseline_share: number; current_share: number; share_delta: number; insufficient_data?: boolean }>;
  insufficient_data?: boolean;
};
