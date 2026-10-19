import { expect, test, type Page } from "@playwright/test";
import { rig, startInvestigation, shops } from "./fixtures";
import { writeFileSync } from "node:fs";
async function choose(page: Page, label: string, option: string) {
  await page.getByLabel(label, { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
    .getByText(option, { exact: true })
    .click();
}
async function startReport(page: Page) {
  await choose(page, "子 ASIN", "B000TEST01 / HB-CHARGER-01");
  await page.getByRole("button", { name: "开始取证", exact: true }).click();
}
test("investigation default, explicit DeepSeek, terminal polling and shop reset", async ({
  page,
}) => {
  const state = await rig(page);
  await page.goto("/investigations");
  await startInvestigation(page);
  await expect(page.getByText("后台任务：执行结束")).toBeVisible();
  expect(
    state.requests.filter(
      (r) => r.path === "investigations/" && r.method === "POST",
    )[0].body.planner_provider,
  ).toBe("deterministic");
  const reads = state.requests.filter((r) => r.path.startsWith("jobs/")).length;
  await page.waitForTimeout(1400);
  expect(state.requests.filter((r) => r.path.startsWith("jobs/")).length).toBe(
    reads,
  );
  await expect(
    page.getByRole("button", { name: "开始调查", exact: true }),
  ).not.toHaveClass(/ant-btn-loading/);
  await choose(page, "调查模式", "DeepSeek 调查（需后端校验）");
  await page.getByRole("button", { name: "开始调查", exact: true }).click();
  await expect(page.getByText("后台任务：执行结束")).toBeVisible();
  const posts = state.requests.filter(
    (r) => r.path === "investigations/" && r.method === "POST",
  );
  expect(posts).toHaveLength(2);
  expect(posts[1].body.planner_provider).toBe("deepseek");
  await expect(page.getByText("实际调查规划模式：规则调查")).toBeVisible();
  await expect(page.getByText(/实际模型扣款：未知/)).toBeVisible();
  await choose(page, "选择店铺", `${shops[1].name} / DE`);
  await expect(page.getByTestId("investigation-evidence")).toHaveCount(0);
  await expect(page.getByLabel("问题", { exact: true })).toHaveValue("");
  writeFileSync(
    "verification/frontend-polish-20261012/investigation-requests.json",
    JSON.stringify(state, null, 2),
  );
  expect(state.errors).toEqual([]);
});
test("second report task replaces result; network read failures retry without POST; store resets result", async ({
  page,
}) => {
  const state = await rig(page);
  await page.goto("/reports");
  await startReport(page);
  await expect(
    page.getByRole("button", { name: "查看刚生成的报告" }),
  ).toBeVisible();
  const reads = state.requests.filter((r) => r.path.startsWith("jobs/")).length;
  await page.waitForTimeout(1500);
  expect(state.requests.filter((r) => r.path.startsWith("jobs/")).length).toBe(
    reads,
  );
  await page.getByRole("button", { name: "查看刚生成的报告" }).click();
  await expect(page.getByText("报告 B000TEST01 · v1")).toBeVisible();
  state.failJobs = true;
  await page.getByRole("button", { name: "开始取证", exact: true }).click();
  await expect(
    page.getByText("任务状态读取失败，已暂停自动刷新"),
  ).toBeVisible();
  await expect(page.getByText("报告 B000TEST01 · v1")).toHaveCount(0);
  const posts = state.requests.filter(
    (r) => r.path === "semantic-runs/" && r.method === "POST",
  ).length;
  const errorReads = state.requests.filter((r) =>
    r.path.startsWith("jobs/"),
  ).length;
  await page.waitForTimeout(1500);
  expect(state.requests.filter((r) => r.path.startsWith("jobs/")).length).toBe(
    errorReads,
  );
  state.failJobs = false;
  await page.getByRole("button", { name: "重新读取状态" }).click();
  await expect(
    page.getByRole("button", { name: "查看刚生成的报告" }),
  ).toBeVisible();
  await page.getByRole("button", { name: "查看刚生成的报告" }).click();
  await expect(page.getByText("报告 B000TEST01 · v2")).toBeVisible();
  expect(
    state.requests.filter(
      (r) => r.path === "semantic-runs/" && r.method === "POST",
    ),
  ).toHaveLength(posts);
  await choose(page, "选择店铺", `${shops[1].name} / DE`);
  await expect(
    page.getByRole("button", { name: "查看刚生成的报告" }),
  ).toHaveCount(0);
  await expect(page.getByText("报告 B000TEST01 · v2")).toHaveCount(0);
  writeFileSync(
    "verification/frontend-polish-20261012/report-requests.json",
    JSON.stringify(state, null, 2),
  );
  expect(state.errors).toEqual([]);
});
test("reports and jobs page 51, shop reset, disappearing last page and visible read retries", async ({
  page,
}) => {
  const state = await rig(page);
  await page.goto("/reports");
  await expect(page.getByText("共 51 条")).toBeVisible();
  await page.locator(".ant-pagination-item-2").click();
  await expect(page.getByText("B000LAST51", { exact: true })).toBeVisible();
  expect(
    state.requests.some((r) => r.path === "reports/?shop=shop-a&page=2"),
  ).toBe(true);
  state.reportCount = 50;
  await page.getByRole("button", { name: "刷新报告列表" }).click();
  await expect(page.getByText("共 50 条")).toBeVisible();
  await expect(page.locator(".ant-pagination-item-active")).toHaveText("1");
  state.failReports = true;
  await page.getByRole("button", { name: "刷新报告列表" }).click();
  await expect(page.getByText("报告列表读取失败").first()).toBeVisible();
  await expect(page.getByText("还没有标准报告")).toHaveCount(0);
  state.failReports = false;
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "重新读取", exact: true }),
  ).toHaveCount(0);
  state.reportCount = 51;
  await page.getByRole("button", { name: "刷新报告列表" }).click();
  await expect(page.getByText("共 51 条")).toBeVisible();
  await page.locator(".ant-pagination-item-2").click();
  await expect(page.getByText("B000LAST51", { exact: true })).toBeVisible();
  await choose(page, "选择店铺", `${shops[1].name} / DE`);
  await expect(page.locator(".ant-pagination-item-active")).toHaveText("1");
  await page.goto("/jobs");
  await expect(page.getByText("共 51 条")).toBeVisible();
  await expect(page.getByText("等待重试", { exact: true })).toBeVisible();
  await expect(
    page.getByText("执行已结束；暂无摘要，请在调查或报告页核查证据。").first(),
  ).toBeVisible();
  await page.locator(".ant-pagination-item-2").click();
  await expect(page.locator(".ant-table-tbody tr.ant-table-row")).toHaveCount(
    1,
  );
  state.jobCount = 50;
  await page.getByRole("button", { name: "刷新任务列表" }).click();
  await expect(page.getByText("共 50 条")).toBeVisible();
  await expect(page.locator(".ant-pagination-item-active")).toHaveText("1");
  expect(state.requests.every((r) => r.method === "GET")).toBe(true);
  writeFileSync(
    "verification/frontend-polish-20261012/pagination-requests.json",
    JSON.stringify(state, null, 2),
  );
  expect(state.errors).toEqual([]);
});
test("neighbor lists expose 51st task, proposal and observation without unbounded reads", async ({
  page,
}) => {
  const state = await rig(page);
  await page.goto("/actions");
  const tables = page.locator(".ant-table-wrapper");
  await expect(tables.nth(0).getByText("共 51 条")).toBeVisible();
  await tables.nth(0).locator(".ant-pagination-item-2").click();
  await expect(
    tables.nth(0).getByText("核查任务 51", { exact: true }),
  ).toBeVisible();
  await tables.nth(1).locator(".ant-pagination-item-2").click();
  await expect(
    tables.nth(1).getByText("提案 51", { exact: true }),
  ).toBeVisible();
  await page.goto("/events");
  await expect(page.getByText("观察线索 1", { exact: true })).toBeVisible();
  await page
    .locator(".ant-table-wrapper")
    .first()
    .locator(".ant-pagination-item-2")
    .click();
  await expect(page.getByText("观察线索 51", { exact: true })).toBeVisible();
  expect(
    state.requests.filter((r) => r.path.startsWith("action-tasks/")).length,
  ).toBe(2);
  expect(
    state.requests.filter((r) => r.path.startsWith("change-proposals/")).length,
  ).toBe(2);
  writeFileSync(
    "verification/frontend-polish-20261012/neighbor-requests.json",
    JSON.stringify(state, null, 2),
  );
  expect(state.errors).toEqual([]);
});
test("generated report read failure offers GET retry without another model task", async ({
  page,
}) => {
  const state = await rig(page);
  state.failDetail = true;
  await page.goto("/reports");
  await startReport(page);
  await expect(page.getByText("报告读取失败")).toBeVisible();
  state.failDetail = false;
  await page.getByRole("button", { name: "重新读取", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "查看刚生成的报告" }),
  ).toBeVisible();
  expect(state.requests.filter((r) => r.method === "POST")).toHaveLength(1);
  expect(state.errors).toEqual([]);
});
for (const width of [390, 768, 1440])
  test(`keyboard focus and local table scroll ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await rig(page);
    await page.goto("/investigations");
    const query = page.getByLabel("问题", { exact: true });
    await query.focus();
    await query.fill("键盘核查包装反馈");
    await page.keyboard.press("Tab");
    await expect(page.getByLabel("调查模式", { exact: true })).toBeFocused();
    const outline = await page
      .getByLabel("调查模式", { exact: true })
      .evaluate(
        (e) => getComputedStyle(e.closest(".ant-select")!).outlineStyle,
      );
    expect(outline).toBe("solid");
    const submit = page.getByRole("button", { name: "开始调查", exact: true });
    await submit.focus();
    await page.keyboard.press("Enter");
    await expect(page.getByTestId("investigation-evidence")).toBeVisible();
    const contrast = await page
      .locator(".ant-tag-cyan, .ant-tag-orange, .ant-tag-gold")
      .evaluateAll((elements) => {
        const luminance = (color: string) => {
          const values = color
            .match(/\d+(?:\.\d+)?/g)!
            .slice(0, 3)
            .map(Number)
            .map((value) => {
              const c = value / 255;
              return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
            });
          return values[0] * 0.2126 + values[1] * 0.7152 + values[2] * 0.0722;
        };
        return elements.map((element) => {
          const style = getComputedStyle(element),
            foreground = luminance(style.color),
            background = luminance(style.backgroundColor);
          return {
            text: element.textContent,
            color: style.color,
            background: style.backgroundColor,
            ratio:
              (Math.max(foreground, background) + 0.05) /
              (Math.min(foreground, background) + 0.05),
          };
        });
      });
    expect(contrast.length).toBeGreaterThan(0);
    writeFileSync(
      `verification/frontend-polish-20261012/contrast-${width}.json`,
      JSON.stringify(contrast, null, 2),
    );
    expect(contrast.every((value) => value.ratio >= 4.5)).toBe(true);
    await page.goto("/reports");
    const select = page.getByLabel("子 ASIN", { exact: true });
    await select.focus();
    await page.keyboard.press("ArrowDown");
    await page.keyboard.press("Enter");
    await page.getByRole("button", { name: "开始取证", exact: true }).focus();
    await page.keyboard.press("Enter");
    await expect(
      page.getByRole("button", { name: "查看刚生成的报告" }),
    ).toBeVisible();
    expect(
      await page.evaluate(() => document.documentElement.scrollWidth),
    ).toBeLessThanOrEqual(width + 1);
    if (width < 1000) {
      const scrolling = page.locator(".ant-table-content").first();
      expect(
        await scrolling.evaluate((e) => e.scrollWidth > e.clientWidth),
      ).toBe(true);
      await scrolling.evaluate((e) => (e.scrollLeft = e.scrollWidth));
      expect(await scrolling.evaluate((e) => e.scrollLeft)).toBeGreaterThan(0);
    }
    expect(state.errors).toEqual([]);
  });
