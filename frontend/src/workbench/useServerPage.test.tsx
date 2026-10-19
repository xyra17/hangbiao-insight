// @vitest-environment jsdom
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import useServerPage from "./useServerPage";
import { ApiError, request } from "./api";
vi.mock("./api", async (original) => ({
  ...(await original<typeof import("./api")>()),
  request: vi.fn(),
}));
const api = vi.mocked(request);
let cache: QueryClient;
const wrapper = ({ children }: { children: React.ReactNode }) => (
  <QueryClientProvider client={cache}>{children}</QueryClientProvider>
);
const rows = Array.from({ length: 51 }, (_, i) => ({ id: i + 1 }));
const pageData = (p: number, count = 51) => ({
  count,
  next: p === 1 && count > 50 ? "?page=2" : null,
  previous: p > 1 ? "?page=1" : null,
  results: rows.slice((p - 1) * 50, Math.min(p * 50, count)),
});
beforeEach(() => {
  cache = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  api.mockReset();
});
afterEach(() => {
  cleanup();
  cache.clear();
});
describe("bounded server pagination", () => {
  it.each([
    "reports",
    "jobs",
    "action-tasks",
    "change-proposals",
    "observations",
  ])(
    "makes the 51st %s reachable without fetching all pages",
    async (endpoint) => {
      api.mockImplementation(async (path) =>
        pageData(
          Number(new URL(path, "http://test/").searchParams.get("page")),
        ),
      );
      const { result } = renderHook(
        () =>
          useServerPage<{ id: number }>({
            key: endpoint,
            scope: "a",
            path: `${endpoint}/?shop=a`,
          }),
        { wrapper },
      );
      await waitFor(() =>
        expect(result.current.data?.results).toHaveLength(50),
      );
      expect(api).toHaveBeenCalledTimes(1);
      expect(result.current.pagination.total).toBe(51);
      act(() => result.current.pagination.onChange(2));
      await waitFor(() => expect(result.current.data?.results[0].id).toBe(51));
      expect(api).toHaveBeenLastCalledWith(`${endpoint}/?shop=a&page=2`);
    },
  );
  it("resets page synchronously on scope changes", async () => {
    api.mockImplementation(async (path) =>
      pageData(Number(new URL(path, "http://test/").searchParams.get("page"))),
    );
    const { result, rerender } = renderHook(
      ({ scope }) =>
        useServerPage({
          key: "reports",
          scope,
          path: `reports/?shop=${scope}`,
        }),
      { wrapper, initialProps: { scope: "a" } },
    );
    await waitFor(() => expect(result.current.data).toBeTruthy());
    act(() => result.current.setPage(2));
    await waitFor(() => expect(result.current.data?.results).toHaveLength(1));
    rerender({ scope: "b" });
    expect(result.current.pagination.current).toBe(1);
    await waitFor(() =>
      expect(api).toHaveBeenLastCalledWith("reports/?shop=b&page=1"),
    );
  });
  it.each(["empty", "404"])(
    "recovers a disappeared last page after invalidation (%s)",
    async (kind) => {
      let count = 51;
      api.mockImplementation(async (path) => {
        const p = Number(
          new URL(path, "http://test/").searchParams.get("page"),
        );
        if (p === 2 && count === 50 && kind === "404")
          throw new ApiError(404, "无效分页");
        return pageData(p, count);
      });
      const { result } = renderHook(
        () =>
          useServerPage({
            key: "reports",
            scope: "a",
            path: "reports/?shop=a",
          }),
        { wrapper },
      );
      await waitFor(() => expect(result.current.data).toBeTruthy());
      act(() => result.current.setPage(2));
      await waitFor(() => expect(result.current.data?.results).toHaveLength(1));
      count = 50;
      await act(async () => {
        await cache.invalidateQueries({ queryKey: ["reports", "a"] });
      });
      await waitFor(() => expect(result.current.pagination.current).toBe(1));
      await waitFor(() =>
        expect(result.current.data?.results).toHaveLength(50),
      );
      expect(result.current.pagination.total).toBe(50);
    },
  );
  it("can revisit a formerly invalid page when new records restore it", async () => {
    let count = 51;
    api.mockImplementation(async (path) => {
      const p = Number(new URL(path, "http://test/").searchParams.get("page"));
      if (p === 2 && count === 50) throw new ApiError(404, "无效分页");
      return pageData(p, count);
    });
    const { result } = renderHook(
      () =>
        useServerPage<{ id: number }>({
          key: "reports",
          scope: "a",
          path: "reports/?shop=a",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.data).toBeTruthy());
    act(() => result.current.setPage(2));
    await waitFor(() => expect(result.current.data?.results).toHaveLength(1));
    count = 50;
    await act(async () => {
      await cache.invalidateQueries({ queryKey: ["reports", "a"] });
    });
    await waitFor(() => expect(result.current.pagination.current).toBe(1));
    await waitFor(() => expect(result.current.data?.count).toBe(50));
    count = 51;
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.data?.count).toBe(51));
    act(() => result.current.setPage(2));
    await waitFor(() => expect(result.current.data?.results[0].id).toBe(51));
    expect(result.current.pagination.current).toBe(2);
  });
  it("preserves page on network errors and allows read-only retry", async () => {
    let fail = false;
    api.mockImplementation(async (path) => {
      if (fail) throw new ApiError(500, "网络异常");
      return pageData(
        Number(new URL(path, "http://test/").searchParams.get("page")),
      );
    });
    const { result } = renderHook(
      () => useServerPage({ key: "jobs", scope: "a", path: "jobs/?shop=a" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.data).toBeTruthy());
    fail = true;
    act(() => result.current.setPage(2));
    await waitFor(() => expect(result.current.error).toBeTruthy());
    expect(result.current.pagination.current).toBe(2);
    fail = false;
    await act(async () => {
      await result.current.refetch();
    });
    await waitFor(() => expect(result.current.data?.results).toHaveLength(1));
    expect(api.mock.calls.every(([, method]) => method === undefined)).toBe(
      true,
    );
  });
});
