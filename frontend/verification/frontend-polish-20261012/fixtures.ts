import { expect, type Page } from "@playwright/test";
export const shops = [
  {
    id: "shop-a",
    name: "航标演示 · 北美店铺",
    marketplace: "US",
    timezone: "America/Los_Angeles",
    currency: "USD",
    is_test: true,
  },
  {
    id: "shop-b",
    name: "航标演示 · 欧洲店铺",
    marketplace: "DE",
    timezone: "Europe/Berlin",
    currency: "EUR",
    is_test: true,
  },
];
export const product = {
  id: "product-a",
  shop: "shop-a",
  child_asin: "B000TEST01",
  seller_sku: "HB-CHARGER-01",
  fulfillment: "FBA",
  version: 1,
  mappings: [
    {
      id: "map-a",
      name: "便携充电器",
      parent_asin: "",
      owner_name: "运营团队",
      version: 1,
      effective_from: "2026-10-01",
      effective_to: null,
    },
  ],
};
export const reports = Array.from({ length: 51 }, (_, i) => ({
  id: `report-${i + 1}`,
  child_asin: i === 50 ? "B000LAST51" : "B000TEST01",
  status: "published",
  report_version: i + 1,
  created_at: "2026-10-12T02:30:00Z",
  payload: {
    summary: "包装受损反馈需结合退款和售后原文进一步核查。",
    status: "partial",
    gaps: ["缺少可比退款来源，暂不能直接归因。"],
    limitations: ["当前报告仅覆盖授权范围内的数据。"],
    claims: [],
    signals: [],
  },
}));
export const jobs = Array.from({ length: 51 }, (_, i) => ({
  id: `history-job-${i + 1}`,
  kind: "investigation_run",
  status: i === 0 ? "retry_wait" : "completed",
  result: { investigation_id: `run-${i}` },
  error: "",
  created_at: "2026-10-12T02:30:00Z",
  attempts: 1,
}));
export const run = {
  id: "run-a",
  query: "请核查便携充电器的包装反馈",
  child_asin: "B000TEST01",
  status: "completed",
  steps_used: 3,
  max_steps: 6,
  stop_reason: "授权范围内的证据已读取，部分来源尚待补充。",
  decisions: [
    {
      phase: "after_reviews",
      next_tool: "stop",
      execution_origin: "local_rule",
    },
  ],
  budget: {
    remaining_units: 3,
    model_attempts: 2,
    model_tokens: 0,
    model_cost: "0",
    elapsed_ms: 300,
    duplicate_queries: 0,
  },
  steps: [],
  result: {
    completion_status: "partial",
    conclusion: { status: "unknown" },
    subtasks: [
      {
        family: "refunds",
        label: "退款",
        status: "blocked",
        reason: "缺少可比来源",
      },
    ],
    observations: [
      {
        fact_id: "fact-a",
        record_id: "review-a",
        raw_row_id: "row-a",
        business_date: "2026-10-05",
        title: "包装有压痕",
        text: "包装盒在到货时有压痕，需要核查运输及包装原因。",
        revision: 1,
        match_kind: "relevant_hit",
      },
    ],
    limitations: ["个案反馈不能直接推断总体发生率。"],
  },
};
export async function rig(page: Page, authenticated = true) {
  const state = {
    authenticated,
    failReports: false,
    failDetail: false,
    failJobs: false,
    reportCount: 51,
    jobCount: 51,
    jobReads: 0,
    submissions: 0,
    requests: [] as { path: string; method: string; body: any }[],
    errors: [] as string[],
    console: [] as string[],
  };
  page.on("pageerror", (e) => state.errors.push(e.message));
  page.on("console", (m) => {
    if (["warning", "error"].includes(m.type()))
      state.console.push(`${m.type()}: ${m.text()}`);
  });
  const paginated = (rows: unknown[], n: number, pageNumber: number) => ({
    count: n,
    next: pageNumber * 50 < n ? `?page=${pageNumber + 1}` : null,
    previous: pageNumber > 1 ? "?page=1" : null,
    results: rows.slice((pageNumber - 1) * 50, Math.min(pageNumber * 50, n)),
  });
  await page.route("**/*", async (route) => {
    const req = route.request();
    const url = new URL(req.url());
    if (url.origin !== "http://127.0.0.1:5197") return route.abort();
    if (!url.pathname.startsWith("/api/v1/")) return route.continue();
    const path = url.pathname.slice("/api/v1/".length);
    const method = req.method();
    let body: any;
    try {
      body = req.postDataJSON();
    } catch {
      body = null;
    }
    state.requests.push({ path: path + url.search, method, body });
    const send = (data: unknown, status = 200) =>
      route.fulfill({
        status,
        contentType: "application/json",
        body: JSON.stringify(data),
      });
    const fail = () =>
      send(
        {
          code: "internal_error",
          message: "受控网络错误，请重新读取。",
          field_errors: {},
        },
        500,
      );
    const p = Number(url.searchParams.get("page") || 1);
    if (path === "session/")
      return send({
        authenticated: state.authenticated,
        username: state.authenticated ? "controlled_preview" : null,
        csrf_token: "fixture-token",
      });
    if (path === "login/") {
      state.authenticated = true;
      return send({ csrf_token: "fixture-token" });
    }
    if (path === "logout/") {
      state.authenticated = false;
      return send({});
    }
    if (path === "shops/") return send(paginated(shops, 2, 1));
    if (/shops\/[^/]+\/capabilities\//.test(path))
      return send({
        roles: ["data_admin", "operator", "quality"],
        is_admin: true,
      });
    if (/shops\/[^/]+\/members\//.test(path)) return send({ items: [] });
    if (path === "products/") return send(paginated([product], 1, p));
    if (path === "reports/") {
      if (state.failReports) return fail();
      if ((p - 1) * 50 >= state.reportCount && p > 1)
        return send({ message: "无效分页", field_errors: {} }, 404);
      return send(paginated(reports, state.reportCount, p));
    }
    if (/reports\/[^/]+\/$/.test(path))
      return state.failDetail
        ? fail()
        : send(reports.find((r) => r.id === path.split("/")[1]) || reports[0]);
    if (path === "jobs/") {
      if (state.failJobs) return fail();
      if ((p - 1) * 50 >= state.jobCount && p > 1)
        return send({ message: "无效分页", field_errors: {} }, 404);
      return send(paginated(jobs, state.jobCount, p));
    }
    if (path.startsWith("jobs/")) {
      state.jobReads++;
      return state.failJobs
        ? fail()
        : send({
            id: path.split("/")[1],
            status: state.jobReads % 2 ? "running" : "completed",
            error: "",
            result: {
              report_id: `report-${state.submissions}`,
              investigation_id: `run-${state.submissions}`,
            },
          });
    }
    if (path === "semantic-runs/" && method === "GET")
      return send(paginated([], 0, 1));
    if (
      path === "semantic-runs/" ||
      (path === "investigations/" && method === "POST")
    ) {
      state.submissions++;
      state.jobReads = 0;
      return send(
        {
          id: `current-job-${state.submissions}`,
          status: "queued",
          error: "",
          result: { investigation_id: `run-${state.submissions}` },
        },
        202,
      );
    }
    if (/investigations\/[^/]+\/$/.test(path))
      return send({ ...run, id: path.split("/")[1] });
    if (path.endsWith("/refreshes/")) return send({ items: [] });
    if (path === "evidence-search/")
      return send({
        retrieval_id: "retrieval-a",
        retrieval_complete: false,
        match_status: "incomplete",
        limitations: ["检索范围未完整覆盖，不能据此判断没有证据。"],
        items: [{ ...run.result.observations[0], match_kind: "future_kind" }],
      });
    if (path === "action-tasks/")
      return send(
        paginated(
          Array.from({ length: 51 }, (_, i) => ({
            id: `task-${i}`,
            title: `核查任务 ${i + 1}`,
            child_asin: "B000TEST01",
            status: "draft",
            review_status: "pending",
            review_comparison: {},
            executions: [],
            version: 1,
          })),
          51,
          p,
        ),
      );
    if (path === "change-proposals/")
      return send(
        paginated(
          Array.from({ length: 51 }, (_, i) => ({
            id: `proposal-${i}`,
            idempotency_key: `提案 ${i + 1}`,
            child_asin: "B000TEST01",
            status: "pending",
            operation: "internal_followup",
            version: 1,
            execution_policy: "internal_task_assignment",
          })),
          51,
          p,
        ),
      );
    if (path === "observations/")
      return send(
        paginated(
          Array.from({ length: 51 }, (_, i) => ({
            id: `observation-${i}`,
            title: `观察线索 ${i + 1}`,
            child_asin: "B000TEST01",
            metric: "refunds",
            last_seen: "2026-10-12",
            snapshot: "snapshot-a",
          })),
          51,
          p,
        ),
      );
    if (path === "events/" || path === "snapshots/")
      return send(paginated([], 0, 1));
    return send(
      {
        code: "fixture_missing",
        message: `未配置受控接口 ${path}`,
        field_errors: {},
      },
      501,
    );
  });
  return state;
}
export async function startInvestigation(page: Page) {
  await page
    .getByLabel("问题", { exact: true })
    .fill("请核查便携充电器的包装反馈");
  await page.getByRole("button", { name: "开始调查", exact: true }).click();
  await expect(page.getByTestId("investigation-evidence")).toBeVisible();
  await expect(page.getByText("后台任务：执行结束")).toBeVisible();
}
