import { expect, test } from "@playwright/test";
import { resolve } from "node:path";
import { accounts } from "./support/accounts";

test("冻结指标无评论时展示两窗分母和业务边界", async ({ page }) => {
  test.setTimeout(180000);
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").waitFor({ timeout: 10000 });
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click({ timeout: 10000 });
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const [asin, state, message] of [["BINV260010", "ratio", "分母：200 会话"], ["BINV260011", "low-sample", "样本不足"], ["BINV260012", "observing", "等待既定观察期"], ["BINV260013", "missing", "日指标缺源"]]) {
      await page.getByLabel("问题", { exact: true }).fill(`核对冻结指标与原始记录 ${asin}`);
      const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
      await page.locator("button[type=submit]").click();
      const response = await responsePromise;
      expect(response.status()).toBe(202);
      const job = await response.json();
      const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
      await expect(evidence).toBeVisible({ timeout: 30000 });
      await expect(evidence.getByText("结论未知", { exact: true })).toBeVisible();
      await expect(evidence).toContainText("冻结指标证据");
      await expect(evidence).toContainText("SYNTHETIC");
      await expect(evidence).toContainText(message);
      await expect(evidence).toContainText("指标事实不能单独证明经营原因");
      await expect(evidence).toContainText("当前调查没有可展示的原文证据");
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
      await page.screenshot({ path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-metric-evidence-${state}.png`), fullPage: true });
    }
  }
});


test("多指标调查保留销量事实和退款缺口，不称整体完成", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.getByLabel("问题", { exact: true }).fill("分别核查销量下降和退款变化 BINV260014");
    const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
    await page.locator("button[type=submit]").click();
    const response = await responsePromise;
    expect(response.status()).toBe(202);
    const job = await response.json();
    const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
    await expect(evidence.getByTestId("investigation-subtasks")).toContainText("部分核查结束，仍有待补事项", {timeout: 30000});
    await expect(evidence).toContainText("销量与流量：冻结事实可用");
    await expect(evidence).toContainText("分母：200 会话");
    await expect(evidence).toContainText("当前窗");
    await expect(evidence).toContainText("退款成熟队列：暂停比较");
    await expect(evidence).toContainText("结论未知");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-partial.png`), fullPage: true});
  }
});


test("发生金额与成熟队列分开显示并保留币种和两窗", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.getByLabel("问题", { exact: true }).fill("核对退款金额和成熟队列退款率 BINV260015");
    const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
    await page.locator("button[type=submit]").click();
    const response = await responsePromise;
    expect(response.status()).toBe(202);
    const job = await response.json();
    const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
    await expect(evidence.getByTestId("investigation-subtasks")).toContainText("部分核查结束", {timeout: 30000});
    await expect(evidence).toContainText("退款发生口径：冻结事实可用");
    await expect(evidence).toContainText("退款成熟队列：暂停比较");
    await expect(evidence).toContainText("退款发生金额：25.00 USD");
    await expect(evidence).toContainText("退款发生金额：8.00 EUR");
    await expect(evidence).toContainText("基准窗");
    await expect(evidence).toContainText("当前窗");
    await expect(evidence).not.toContainText("分母：");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-occurrence.png`), fullPage: true});
  }
});


test("授权逐订单工具显示缺源，不把指标或评论当成订单明细", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.getByLabel("问题", { exact: true }).fill("逐单核查订单明细 BINV260015");
    const pending = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
    await page.locator("button[type=submit]").click();
    const response = await pending;
    expect(response.status()).toBe(202);
    const job = await response.json();
    const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
    await expect(evidence.getByTestId("order-records-evidence")).toBeVisible({timeout: 30000});
    await expect(evidence.getByTestId("order-records-evidence")).toContainText("订单与发货：缺少来源");
    await expect(evidence.getByTestId("order-records-evidence")).toContainText("退款交易：缺少来源");
    await expect(evidence.getByTestId("order-records-evidence")).toContainText("退货记录：缺少来源");
    await expect(evidence).toContainText("当前请求仍有前置缺口");
    await expect(evidence).toContainText("每类来源最多20条");
    await expect(evidence).toContainText("结论未知");
    await expect(evidence).not.toContainText("缺少授权核查工具");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-order-source-gap.png`), fullPage: true});
  }
});


test("显式请求错窗在三宽展示请求与实际窗口并隐藏发生金额", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click();
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.getByLabel("问题", { exact: true }).fill("核对2025年1月退款金额 BINV260015");
    const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
    await page.locator("button[type=submit]").click();
    const response = await responsePromise;
    expect(response.status()).toBe(202);
    const job = await response.json();
    const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
    await expect(evidence.getByTestId("investigation-request-scope")).toContainText("2025-01-01 — 2025-01-31", {timeout: 30000});
    await expect(evidence).toContainText("实际可用窗口");
    await expect(evidence).toContainText("不替换为默认两窗");
    await expect(evidence).not.toContainText("退款发生金额：25.00 USD");
    await expect(evidence).not.toContainText("退款发生金额：8.00 EUR");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-date-mismatch.png`), fullPage: true});
  }
});

test("未解析相对日期先澄清，不读取默认窗口", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  await dropdown.locator(".ant-select-dropdown-list-holder").evaluate((element) => { element.scrollTop = element.scrollHeight; });
  await dropdown.getByText("验收 G：冻结指标证据 / US", { exact: true }).click();
  let parentId = "";
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({width, height: 1000});
    await page.getByLabel("问题", { exact: true }).fill("检查最近两周退款金额和订单明细 BINV260015");
    const responsePromise = page.waitForResponse((response) => response.url().endsWith("/api/v1/investigations/") && response.request().method() === "POST");
    await page.locator("button[type=submit]").click();
    const response = await responsePromise;
    expect(response.status()).toBe(202);
    const job = await response.json();
    parentId = job.result.investigation_id;
    const evidence = page.locator(`[data-testid="investigation-evidence"][data-run-id="${job.result.investigation_id}"]`);
    await expect(evidence.getByTestId("investigation-request-scope")).toContainText("等待明确起止日期", {timeout: 30000});
    await expect(evidence).not.toContainText("实际可用窗口");
    await expect(evidence).toContainText("不会改用默认窗口");
    await expect(page.getByTestId("investigation-trajectory")).not.toContainText("allowed");
    await expect(evidence).not.toContainText("退款发生金额：25.00 USD");
    await expect(evidence).not.toContainText("退款发生金额：8.00 EUR");
    expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
    await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, `${width}-investigation-date-clarification.png`), fullPage: true});
  }
  await page.getByPlaceholder("补充 SKU、名称或范围").fill("2025-01-01至2025-01-31");
  const clarifiedPromise = page.waitForResponse((response) => response.url().endsWith(`/investigations/${parentId}/clarify/`) && response.request().method() === "POST");
  await page.getByRole("button", {name: "澄清后继续", exact: true}).click();
  const clarified = await clarifiedPromise;
  expect(clarified.status()).toBe(202);
  const next = await clarified.json();
  const continued = page.locator(`[data-testid="investigation-evidence"][data-run-id="${next.result.investigation_id}"]`);
  await expect(continued.getByTestId("investigation-request-scope")).toContainText("2025-01-01 — 2025-01-31", {timeout: 30000});
  await expect(continued).toContainText("实际可用窗口");
  await expect(continued).not.toContainText("等待明确起止日期");
  await page.screenshot({path: resolve(process.env.ACCEPTANCE_SCREENSHOT_DIR!, "1440-investigation-date-continued.png"), fullPage: true});

});
