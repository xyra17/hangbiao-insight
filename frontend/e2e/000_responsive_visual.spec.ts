import { mkdirSync } from "node:fs";
import { resolve } from "node:path";
import { expect, test } from "@playwright/test";
import { accounts } from "./support/accounts";

const screenshotDir = resolve(
  process.env.ACCEPTANCE_SCREENSHOT_DIR ?? "test-results/responsive",
);
mkdirSync(screenshotDir, { recursive: true });

test("真实 API 页面在 390、768、1440 宽度下可浏览并保留证据抽屉", async ({ page, browser }) => {
  test.setTimeout(120000);
  page.setDefaultTimeout(15000);
  const admin = accounts.find((account) => account.username === "accept_admin");
  expect(admin, "acceptance administrator credentials are required").toBeTruthy();

  const serverErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on("response", (response) => {
    if (response.url().includes("/api/v1/") && response.status() >= 500) {
      serverErrors.push(`${response.status()} ${response.url()}`);
    }
  });
  page.on("pageerror", (error) => pageErrors.push(error.message));

  for (const width of [1440, 768, 390]) {
    const loginContext = await browser.newContext({ viewport: { width, height: 1000 } });
    const loginPage = await loginContext.newPage();
    await loginPage.goto("/events");
    await expect(loginPage.getByLabel("用户名", { exact: true })).toBeVisible();
    const loginWidth = await loginPage.evaluate(() => document.documentElement.scrollWidth);
    expect(loginWidth, `login should not overflow at ${width}px`).toBeLessThanOrEqual(width + 1);
    await loginPage.screenshot({ path: resolve(screenshotDir, `${width}-login.png`), fullPage: true });
    await loginContext.close();
  }

  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(admin!.username);
  await page.getByLabel("密码", { exact: true }).fill(admin!.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "事件队列", exact: true })).toBeVisible();

  const chooseShop = async (label: string) => {
    await page.getByLabel("选择店铺", { exact: true }).click();
    await page
      .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
      .getByText(label, { exact: true })
      .click();
  };
  const capture = async (name: string) => {
    await page.screenshot({
      path: resolve(screenshotDir, name),
      fullPage: true,
      animations: "disabled",
    });
  };
  const checkNoPageOverflow = async (width: number) => {
    const documentWidth = await page.evaluate(() => document.documentElement.scrollWidth);
    expect(documentWidth, `document should not overflow at ${width}px`).toBeLessThanOrEqual(width + 1);
  };

  await page.setViewportSize({ width: 1440, height: 1000 });
  await chooseShop("测试店铺 A / US");
  await expect(page.locator(".wb-shop-select .ant-select-content-has-value")).toHaveAttribute(
    "title",
    "测试店铺 A / US",
  );

  for (const width of [1440, 768, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.goto("/events");
    await expect(page.getByRole("heading", { name: "事件队列", exact: true })).toBeVisible();
    await page.waitForLoadState("networkidle");

    const navTrigger = page.getByRole("button", { name: "切换导航菜单", exact: true });
    if (width < 992) {
      await expect(navTrigger).toBeVisible();
      await navTrigger.press("Enter");
      const eventNav = page.getByRole("menuitem", { name: "事件队列", exact: true });
      await expect(eventNav).toBeVisible();
      await expect(navTrigger).toHaveAttribute("aria-expanded", "true");
      await capture(`${width}-navigation-open.png`);
      await eventNav.click();
      await expect(navTrigger).toHaveAttribute("aria-expanded", "false");
    } else {
      await expect(page.getByRole("menuitem", { name: "事件队列", exact: true })).toBeVisible();
      await capture(`${width}-navigation.png`);
    }

    await capture(`${width}-events.png`);
    await checkNoPageOverflow(width);
    const eventRow = page
      .getByRole("row")
      .filter({ hasText: "B000TEST01" })
      .filter({ hasText: "合成流程验收事件" });
    await expect(eventRow).toHaveCount(1);
    const evidenceResponsePromise = page.waitForResponse((response) =>
      response.url().includes("/api/v1/events/") && response.url().endsWith("/evidence/"),
    );
    await eventRow.getByRole("button", { name: "查看证据", exact: true }).click();
    await expect(page.getByText("事件证据", { exact: true })).toBeVisible();
    const evidenceResponse = await evidenceResponsePromise;
    expect(evidenceResponse.status()).toBe(200);
    const evidencePayload = await evidenceResponse.json();
    expect(evidencePayload.results).toEqual(
      expect.arrayContaining([expect.objectContaining({ metric: "ordered_units" })]),
    );
    await expect(page.getByRole("cell", { name: "订购件数", exact: true }).first()).toBeVisible();
    const evidenceTable = page.locator(".wb-event-evidence-drawer .ant-table-content:visible").first();
    const evidenceCell = evidenceTable.locator("tbody tr:not(.ant-table-measure-row) td").first();
    await expect(evidenceCell).toHaveCSS("white-space", "nowrap");
    if (width === 390) {
      const scrollable = await evidenceTable.evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));
      expect(scrollable.scrollWidth).toBeGreaterThan(scrollable.clientWidth);
      await evidenceTable.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
      expect(await evidenceTable.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
      await evidenceTable.evaluate((element) => { element.scrollLeft = 0; });
    }
    await capture(`${width}-event-evidence-drawer.png`);
    await page.locator(".ant-drawer-close").click();

    const actionResponsePromise = page.waitForResponse((response) =>
      response.url().includes("/api/v1/action-tasks/?shop="),
    );
    await page.goto("/actions");
    await expect(page.getByRole("heading", { name: "动作与复查", exact: true })).toBeVisible();
    const actionPayload = await (await actionResponsePromise).json();
    await page.waitForLoadState("networkidle");
    expect(actionPayload.results).toEqual(
      expect.arrayContaining([expect.objectContaining({ title: "S5 复查展示验收" })]),
    );
    await expect(page.getByRole("row").filter({ hasText: "S5 复查展示验收" })).toHaveCount(1);
    const reviewRow = page.getByRole("row").filter({ hasText: "S5 复查展示验收" });
    const review = actionPayload.results.find(
      (item: { title: string }) => item.title === "S5 复查展示验收",
    );
    const comparison = review.review_comparison.returns;
    const baselineStart = comparison.window_evidence?.baseline?.start ?? comparison.window_start;
    const baselineEnd = comparison.window_evidence?.baseline?.end ?? comparison.window_end;
    const currentStart = comparison.window_evidence?.current?.start ?? comparison.window_start;
    const currentEnd = comparison.window_evidence?.current?.end ?? comparison.window_end;
    if (width === 390) {
      await expect(reviewRow).not.toContainText("成熟退货订单率");
      const compactRowHeight = await reviewRow.evaluate((row) => row.getBoundingClientRect().height);
      expect(compactRowHeight, "mobile action rows should stay compact until details are requested").toBeLessThan(200);
      await reviewRow.getByRole("button", { name: "复查详情", exact: true }).click();
      const reviewDialog = page.getByRole("dialog");
      await expect(reviewDialog).toContainText(`基线窗口：${baselineStart} 至 ${baselineEnd}`);
      await expect(reviewDialog).toContainText(`观察窗口：${currentStart} 至 ${currentEnd}`);
      await expect(reviewDialog).toContainText("成熟订单分母：100 → 100 单");
      await page.locator(".ant-modal-close").click();
      await page.evaluate(() => window.scrollTo(0, 0));
      await page.locator(".ant-table-content").first().evaluate((table) => { table.scrollLeft = 0; });
    } else {
      await expect(reviewRow).toContainText("成熟退货订单率");
      await expect(reviewRow).toContainText(`基线窗口：${baselineStart} 至 ${baselineEnd}`);
      await expect(reviewRow).toContainText(`观察窗口：${currentStart} 至 ${currentEnd}`);
      await expect(reviewRow).toContainText("成熟订单分母：100 → 100 单");
    }
    await expect(reviewRow).not.toContainText("未知 件");
    await capture(`${width}-actions.png`);
    if (width === 390) {
      const taskTable = page.locator(".ant-table-content").first();
      const tableWidth = await taskTable.evaluate((element) => ({
        clientWidth: element.clientWidth,
        scrollWidth: element.scrollWidth,
      }));
      expect(tableWidth.scrollWidth).toBeGreaterThan(tableWidth.clientWidth);
      await taskTable.evaluate((element) => { element.scrollLeft = element.scrollWidth; });
      expect(await taskTable.evaluate((element) => element.scrollLeft)).toBeGreaterThan(0);
      await capture(`${width}-actions-scrolled.png`);
      await taskTable.evaluate((element) => { element.scrollLeft = 0; });
    }
    await checkNoPageOverflow(width);

    await page.goto("/investigations");
    await expect(page.getByRole("heading", { name: "自主调查", exact: true })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await capture(`${width}-investigations.png`);
    await checkNoPageOverflow(width);

    await page.goto("/sources");
    await expect(page.getByRole("heading", { name: "数据源", exact: true })).toBeVisible();
    await page.waitForLoadState("networkidle");
    await capture(`${width}-sources.png`);
    await checkNoPageOverflow(width);
  }

  expect(serverErrors, "browser pages should not receive server errors from the API").toEqual([]);
  expect(pageErrors, "browser pages should not produce runtime errors").toEqual([]);
});
