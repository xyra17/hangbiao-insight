import type { components } from "./schema";
export type Session = components["schemas"]["Session"];
export type Page<T> = {
  count: number;
  next: string | null;
  previous: string | null;
  results: T[];
};
export type Shop = components["schemas"]["Shop"];
export type Mapping = components["schemas"]["Mapping"];
export type Product = components["schemas"]["Product"];
export type Job = components["schemas"]["Job"];
let csrf = "";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

export async function request<T>(
  path: string,
  method = "GET",
  body?: unknown,
): Promise<T> {
  const response = await fetch(`/api/v1/${path}`, {
    method,
    credentials: "same-origin",
    headers: {
      ...(body instanceof FormData
        ? {}
        : { "Content-Type": "application/json" }),
      ...(method !== "GET" ? { "X-CSRFToken": csrf } : {}),
    },
    body:
      body === undefined
        ? undefined
        : body instanceof FormData
          ? body
          : JSON.stringify(body),
  });
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    if (response.status === 403 && path !== "session/" && path !== "login/") {
      const session = await fetch("/api/v1/session/", {
        credentials: "same-origin",
      });
      if (session.ok) {
        const state: Session = await session.json();
        if (!state.authenticated) {
          csrf = state.csrf_token;
          window.dispatchEvent(
            new CustomEvent("workbench-session-expired", { detail: state }),
          );
        }
      }
    }
    const fields = data.field_errors ?? {};
    const detail = Object.entries(fields)
      .filter(([key]) => key !== "detail")
      .map(([key, value]) => `${key}: ${String(value)}`)
      .join("；");
    throw new ApiError(
      response.status,
      detail || data.message || "服务暂时不可用，请稍后重试。",
    );
  }
  if (data.csrf_token) csrf = data.csrf_token;
  return data as T;
}

export const latestMapping = (product: Product): Mapping =>
  [...product.mappings].sort((a, b) => b.version - a.version)[0];

export async function allPages<T>(path: string): Promise<T[]> {
  const rows: T[] = [];
  for (let page = 1; ; page++) {
    const response = await request<Page<T>>(
      `${path}${path.includes("?") ? "&" : "?"}page=${page}`,
    );
    rows.push(...response.results);
    if (!response.next) return rows;
  }
}
