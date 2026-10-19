// @vitest-environment jsdom
import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
} from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";
import EvidenceSearch from "./EvidenceSearch";
import { request, type Product } from "./api";
vi.mock("./api", () => ({ request: vi.fn() }));
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
    value: () => ({
      matches: false,
      addListener() {},
      removeListener() {},
      addEventListener() {},
      removeEventListener() {},
    }),
  });
});
afterEach(cleanup);
describe("retrieval item relevance boundaries", () => {
  it.each([
    ["future_kind", "相关性待确认"],
    [undefined, "相关性待确认"],
    ["scope_context", "仅范围上下文"],
    ["relevant_hit", "相关命中"],
  ])("renders %s without inventing relevance", async (kind, label) => {
    const cache = new QueryClient();
    vi.mocked(request).mockResolvedValue({
      retrieval_id: "test",
      retrieval_complete: false,
      limitations: [],
      items: [{ fact_id: "f1", text: "包装反馈", match_kind: kind }],
    });
    render(
      <QueryClientProvider client={cache}>
        <EvidenceSearch
          shopId="shop"
          products={[{ child_asin: "B000TEST01" } as Product]}
        />
      </QueryClientProvider>,
    );
    fireEvent.mouseDown(screen.getByLabelText("查询商品"));
    await waitFor(() =>
      expect(
        document.querySelector(".ant-select-item-option-content"),
      ).toBeTruthy(),
    );
    fireEvent.click(document.querySelector(".ant-select-item-option-content")!);
    fireEvent.change(screen.getByLabelText("开始日期"), {
      target: { value: "2026-10-01" },
    });
    fireEvent.change(screen.getByLabelText("结束日期"), {
      target: { value: "2026-10-12" },
    });
    fireEvent.change(screen.getByLabelText("问题词或原文短语"), {
      target: { value: "包装" },
    });
    fireEvent.click(screen.getByRole("button", { name: "查询证据" }));
    expect(
      await screen.findByText(label, { selector: ".ant-tag" }),
    ).toBeTruthy();
    expect(screen.getByText("检索未完成，不能据此判断没有证据")).toBeTruthy();
    if (kind !== "relevant_hit")
      expect(
        screen.queryByText("相关命中", { selector: ".ant-tag" }),
      ).toBeNull();
    cache.clear();
  });
});
