// @vitest-environment jsdom
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import {
  afterEach,
  beforeAll,
  beforeEach,
  describe,
  expect,
  it,
  vi,
} from "vitest";
import InvestigationPage from "./InvestigationPage";
import ReportsPage from "./ReportsPage";
import { request, type Product, type Shop } from "./api";
vi.mock("./api", async (importOriginal) => ({ ...await importOriginal<typeof import("./api")>(), request: vi.fn() }));
vi.mock("./EvidenceSearch", () => ({ default: () => null }));
vi.mock("./RefreshHistory", () => ({
  default: ({ onOpen }: { onOpen: (id: string) => void }) => (
    <button onClick={() => onOpen("revision-old")}>打开历史版本测试</button>
  ),
}));
const api = vi.mocked(request);
const shop = { id: "shop-a" } as Shop;
const products = [{ child_asin: "B000TEST01", seller_sku: "SKU" }] as Product[];
let cache: QueryClient;
const job = (id: string, status: string, result = {}, error = "") => ({
  id,
  status,
  result,
  error,
});
const report = {
  id: "report-a",
  child_asin: "B000TEST01",
  status: "published",
  report_version: 1,
  payload: {},
  created_at: "2026-10-12",
};
const sleep = (ms: number) =>
  act(async () => {
    await new Promise((resolve) => setTimeout(resolve, ms));
  });
function mount(page: React.ReactNode) {
  return render(
    <QueryClientProvider client={cache}>{page}</QueryClientProvider>,
  );
}
async function startReport() {
  fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
  fireEvent.click(await screen.findByText("B000TEST01 / SKU"));
  fireEvent.click(screen.getByRole("button", { name: "开始取证" }));
}
async function startInvestigation() {
  fireEvent.change(
    screen.getByPlaceholderText("例如：B000TEST01 最近退货异常的原因是什么？"),
    { target: { value: "退货原因" } },
  );
  fireEvent.click(screen.getByRole("button", { name: "开始调查" }));
}
beforeAll(() => {
  vi.stubGlobal(
    "ResizeObserver",
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    },
  );
  Object.defineProperty(window, "matchMedia", {
    writable: true,
    value: vi.fn().mockImplementation(() => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
      dispatchEvent() {},
    })),
  });
  const getComputedStyle = window.getComputedStyle;
  window.getComputedStyle = (element) => getComputedStyle(element);
});
beforeEach(() => {
  cache = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  api.mockReset();
});
afterEach(() => {
  cleanup();
  cache.clear();
});
describe("investigation mode and state workflow", () => {
  it("sends explicit conservative default without requesting DeepSeek implicitly", async () => {
    api.mockResolvedValue(job("job-a", "failed", {}, "未执行"));
    mount(<InvestigationPage shop={shop} products={products} />);
    await startInvestigation();
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        "investigations/",
        "POST",
        expect.objectContaining({
          shop: "shop-a",
          planner_provider: "deterministic",
        }),
      ),
    );
    expect(
      api.mock.calls.filter(([, method]) => method === "POST"),
    ).toHaveLength(1);
    expect(await screen.findByText("后台任务：执行失败；未执行")).toBeTruthy();
  });
  it("requests DeepSeek after explicit selection and shows rejection without fallback", async () => {
    api.mockRejectedValue(new Error("供应商未启用；请联系管理员确认传输授权"));
    mount(<InvestigationPage shop={shop} products={products} />);
    fireEvent.mouseDown(screen.getAllByRole("combobox")[0]);
    fireEvent.click(await screen.findByText("DeepSeek 调查（需后端校验）"));
    await startInvestigation();
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith(
        "investigations/",
        "POST",
        expect.objectContaining({ planner_provider: "deepseek" }),
      ),
    );
    expect(
      await screen.findByText("供应商未启用；请联系管理员确认传输授权"),
    ).toBeTruthy();
    expect(api).toHaveBeenCalledTimes(1);
  });
  it("stops investigation job and run polling after terminal states", async () => {
    let jobReads = 0;
    let runReads = 0;
    api.mockImplementation(async (path) => {
      if (path === "investigations/") return job("job-a", "queued");
      if (path === "jobs/job-a/") {
        jobReads++;
        return job("job-a", "completed", { investigation_id: "run-a" });
      }
      runReads++;
      return {
        id: "run-a",
        status: "partial",
        steps_used: 2,
        max_steps: 6,
        steps: [],
        result: {},
        decisions: [{ execution_origin: "local_rule" }],
      };
    });
    mount(<InvestigationPage shop={shop} products={products} />);
    await startInvestigation();
    expect(
      await screen.findByText("部分完成", { selector: ".ant-tag" }),
    ).toBeTruthy();
    expect(screen.getByText("实际调查规划模式：规则调查")).toBeTruthy();
    await sleep(1300);
    expect(jobReads).toBe(1);
    expect(runReads).toBe(1);
  });
  it("keeps incomplete business checks distinct from completed execution without enabling unsupported resume", async () => {
    api.mockImplementation(async (path) =>
      path === "investigations/"
        ? job("job-a", "completed", { investigation_id: "run-a" })
        : {
            id: "run-a",
            status: "completed",
            steps_used: 2,
            max_steps: 6,
            steps: [],
            result: { completion_status: "partial" },
          },
    );
    mount(<InvestigationPage shop={shop} products={products} />);
    await startInvestigation();
    expect(
      await screen.findByText("部分完成（执行已结束）", {
        selector: ".ant-tag",
      }),
    ).toBeTruthy();
    expect(
      (
        screen.getByRole("button", {
          name: "接续（沿用原模式）",
        }) as HTMLButtonElement
      ).disabled,
    ).toBe(true);
  });
  it.each([
    ["completed", "正常完成"],
    ["partial", "部分完成"],
    ["failed", "执行失败"],
    ["cancelled", "已取消"],
    ["needs_clarification", "待澄清"],
  ])(
    "renders %s with unconfirmed cost and execution mode kept unknown",
    async (status, label) => {
      api.mockImplementation(async (path) =>
        path === "investigations/"
          ? job("job-a", "completed", { investigation_id: "run-a" })
          : {
              id: "run-a",
              status,
              steps_used: 2,
              max_steps: 6,
              stop_reason: "后端说明",
              steps: [],
              result: {},
              decisions: [{ planner: "deepseek-agent-planner-v3" }],
              budget: {
                remaining_units: 4,
                model_attempts: 1,
                model_tokens: 0,
                model_cost: "0",
                elapsed_ms: 1,
                duplicate_queries: 0,
                entries: [{ metadata: { failure_code: "output_truncated" } }],
              },
            },
      );
      mount(<InvestigationPage shop={shop} products={products} />);
      await startInvestigation();
      expect(
        await screen.findByText(label, { selector: ".ant-tag" }),
      ).toBeTruthy();
      expect(screen.getByText(/实际调查规划模式：未知/)).toBeTruthy();
      expect(screen.getByText(/实际模型扣款：未知/)).toBeTruthy();
      expect(screen.getByText(/模型输出达到上限而被截断/)).toBeTruthy();
      if (status === "completed")
        expect(
          screen.getByText(/调查执行已结束；结论是否成立仍须核查/),
        ).toBeTruthy();
      if (status === "needs_clarification")
        expect(screen.getByRole("button", { name: "澄清后继续" })).toBeTruthy();
    },
  );
});
describe("semantic report job lifecycle", () => {
  it.each(["completed", "failed"])(
    "polls through active states then stops at %s and refreshes both lists",
    async (terminal) => {
      let reads = 0;
      api.mockImplementation(async (path, method) => {
        if (path === "semantic-runs/" && method === "POST")
          return job("job-a", "queued");
        if (path === "jobs/job-a/")
          return job(
            "job-a",
            ["running", "retry_wait", terminal][Math.min(reads++, 2)],
            terminal === "completed" ? { report_id: "report-a" } : {},
            terminal === "failed" ? "后端失败说明" : "",
          );
        if (path === "reports/report-a/") return report;
        return { results: [], count: 0 };
      });
      mount(<ReportsPage shop={shop} products={products} />);
      await startReport();
      await waitFor(() => expect(reads).toBe(3), { timeout: 6000 });
      await waitFor(() => {
        expect(
          api.mock.calls.filter(([path]) => path === "reports/?shop=shop-a&page=1")
            .length,
        ).toBeGreaterThan(1);
        expect(
          api.mock.calls.filter(
            ([path]) => path === "semantic-runs/?shop=shop-a",
          ).length,
        ).toBeGreaterThan(2);
      });
      await sleep(1500);
      expect(reads).toBe(3);
      if (terminal === "completed")
        expect(
          screen.getByRole("button", { name: "查看刚生成的报告" }),
        ).toBeTruthy();
      else
        expect(
          screen.queryByRole("button", { name: "查看刚生成的报告" }),
        ).toBeNull();
    },
    10000,
  );
  it("follows a second successful task without carrying the first report forward", async () => {
    let submissions = 0;
    let finishSecond: (value: unknown) => void = () => {};
    api.mockImplementation(async (path, method) => {
      if (path === "semantic-runs/" && method === "POST")
        return ++submissions === 1
          ? job("job-a", "completed", { report_id: "report-a" })
          : job("job-b", "queued");
      if (path === "jobs/job-b/")
        return new Promise((resolve) => {
          finishSecond = resolve;
        });
      if (path === "reports/report-a/") return report;
      if (path === "reports/report-b/")
        return { ...report, id: "report-b", report_version: 2 };
      return { results: [], count: 0 };
    });
    mount(<ReportsPage shop={shop} products={products} />);
    await startReport();
    expect(
      await screen.findByRole("button", { name: "查看刚生成的报告" }),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "开始取证" }));
    await waitFor(() => expect(api).toHaveBeenCalledWith("jobs/job-b/"));
    expect(
      screen.queryByRole("button", { name: "查看刚生成的报告" }),
    ).toBeNull();
    await act(async () =>
      finishSecond(job("job-b", "completed", { report_id: "report-b" })),
    );
    fireEvent.click(
      await screen.findByRole("button", { name: "查看刚生成的报告" }),
    );
    expect(screen.getByText("报告 B000TEST01 · v2")).toBeTruthy();
    expect(
      api.mock.calls.filter(([path]) => path === "reports/report-a/"),
    ).toHaveLength(1);
  });
  it("clears previous generated and selected report on another submission, including rejection and a late revision", async () => {
    let submissions = 0;
    let rejectSecond: (reason: Error) => void = () => {};
    let resolveRevision: (value: unknown) => void = () => {};
    api.mockImplementation(async (path, method) => {
      if (path === "semantic-runs/" && method === "POST") {
        if (submissions++ === 0)
          return job("job-a", "completed", { report_id: "report-a" });
        return new Promise((_, reject) => {
          rejectSecond = reject;
        });
      }
      if (path === "reports/report-a/") return report;
      if (path === "reports/revision-old/")
        return new Promise((resolve) => {
          resolveRevision = resolve;
        });
      return { results: [], count: 0 };
    });
    mount(<ReportsPage shop={shop} products={products} />);
    await startReport();
    fireEvent.click(
      await screen.findByRole("button", { name: "查看刚生成的报告" }),
    );
    expect(screen.getByText("报告 B000TEST01 · v1")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "打开历史版本测试" }));
    await waitFor(() =>
      expect(api).toHaveBeenCalledWith("reports/revision-old/"),
    );
    fireEvent.click(screen.getByRole("button", { name: "开始取证" }));
    await waitFor(() => expect(submissions).toBe(2));
    expect(
      screen.queryByRole("button", { name: "查看刚生成的报告" }),
    ).toBeNull();
    expect(screen.queryByText("报告 B000TEST01 · v1")).toBeNull();
    await act(async () => rejectSecond(new Error("拒绝第二个任务")));
    expect(await screen.findByText("拒绝第二个任务")).toBeTruthy();
    await act(async () => resolveRevision(report));
    expect(screen.queryByText("报告 B000TEST01 · v1")).toBeNull();
    expect(
      screen.queryByRole("button", { name: "查看刚生成的报告" }),
    ).toBeNull();
  });
  it("pauses on network failure and manually retries status reads only", async () => {
    let reads = 0;
    api.mockImplementation(async (path, method) => {
      if (path === "semantic-runs/" && method === "POST")
        return job("job-a", "queued");
      if (path === "jobs/job-a/") {
        reads++;
        throw new Error("网络中断");
      }
      return { results: [], count: 0 };
    });
    mount(<ReportsPage shop={shop} products={products} />);
    await startReport();
    expect(
      await screen.findByText("任务状态读取失败，已暂停自动刷新"),
    ).toBeTruthy();
    await sleep(1500);
    expect(reads).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: "重新读取状态" }));
    await waitFor(() => expect(reads).toBe(2));
    expect(
      api.mock.calls.filter(([, method]) => method === "POST"),
    ).toHaveLength(1);
  });
  it("ignores previous shop in-flight result after switching shops", async () => {
    let resolveOld: (value: unknown) => void = () => {};
    api.mockImplementation(async (path, method) => {
      if (method === "POST")
        return new Promise((resolve) => {
          resolveOld = resolve;
        });
      if (path === "reports/report-a/") return report;
      return { results: [], count: 0 };
    });
    const view = mount(<ReportsPage shop={shop} products={products} />);
    await startReport();
    await waitFor(() =>
      expect(api.mock.calls.some(([, method]) => method === "POST")).toBe(true),
    );
    view.rerender(
      <QueryClientProvider client={cache}>
        <ReportsPage shop={{ id: "shop-b" } as Shop} products={[]} />
      </QueryClientProvider>,
    );
    await act(async () =>
      resolveOld(job("job-a", "completed", { report_id: "report-a" })),
    );
    expect(
      screen.queryByRole("button", { name: "查看刚生成的报告" }),
    ).toBeNull();
    expect(api.mock.calls.some(([path]) => path === "reports/report-a/")).toBe(
      false,
    );
    expect(
      api.mock.calls.some(([path]) => path === "reports/?shop=shop-b&page=1"),
    ).toBe(true);
  });
});
