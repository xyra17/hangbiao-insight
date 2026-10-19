import { expect, test } from "@playwright/test";
import { resolve } from "node:path";
import { accounts } from "./support/accounts";

test("真实调查在三种宽度下区分相关命中、范围上下文和未命中", async ({ page }) => {
  test.setTimeout(180000);
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "自主调查", exact: true })).toBeVisible();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const shopDropdown = page.locator(".ant-select-dropdown:visible");
  await shopDropdown.locator(".ant-select-dropdown-list-holder").waitFor({ timeout: 10000 });
  await shopDropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await shopDropdown.getByText("验收 F：调查证据边界 / US", { exact: true }).click({ timeout: 10000 });
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const [state, query] of [["related", "请查找包装评论原文"], ["empty", "请查找香味彗星评论原文"], ["context", "总结评论"], ["mixed", "confirmed overheating"]]) {
      await page.getByLabel("问题", { exact: true }).fill(query);
      await expect(page.locator("button[type=submit].ant-btn-loading")).toHaveCount(0);
      const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
      await page.locator("button[type=submit]").click();
      const response = await responsePromise;
      expect(response.status()).toBe(202);
      const job = await response.json();
      const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
      await expect(evidence).toBeVisible({ timeout: 30000 });
      await expect(evidence.getByText("结论未知", { exact: true })).toBeVisible();
      if (state === "related") {
        await expect(evidence.getByText("相关命中", { exact: true }).first()).toBeVisible();
        await expect(evidence).toContainText("包装有压痕");
        await expect(evidence).toContainText("核实个案");
        await expect(evidence).toContainText(/来源版本 [1-9]\d*/ );
        await expect(evidence).toContainText("支持/反证性质：待判断");
      } else if (state === "mixed") {
        await expect(evidence).toContainText("No overheating occurred on Monday.");
        await expect(evidence).toContainText("Overheating occurred on Tuesday.");
        await expect(evidence).toContainText("否定陈述");
        await expect(evidence).toContainText("肯定陈述");
        await expect(evidence).toContainText("标题语境");
        await expect(evidence).toContainText("During supervised use. Synthetic title-only evidence acceptance record.");
      } else if (state === "context") {
        await expect(evidence.getByText("仅范围上下文", { exact: true }).first()).toBeVisible();
        await expect(evidence).toContainText("词法查询没有主题命中");
      } else {
        await expect(evidence).toContainText("当前调查没有可展示的原文证据");
        await expect(evidence).toContainText("请求同一授权对象及日期范围的相关原始记录");
        await expect(evidence).toContainText("相关命中：0 次检索");
      }
      const steps = await page.getByTestId("investigation-trajectory").getByRole("button").allTextContents();
      const sequences = steps.map((text) => Number(text.split(".")[0]));
      expect(sequences).toEqual([...sequences].sort((a, b) => a - b));
      expect(sequences.length).toBeGreaterThan(0);
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
      await page.screenshot({ path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-${state}.png`), fullPage: true });
    }
  }
});
