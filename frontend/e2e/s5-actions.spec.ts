import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const accounts = JSON.parse(
  readFileSync(new URL("../../.local/acceptance-accounts.json", import.meta.url), "utf8"),
);

test("S5 运营可修订开放任务草稿并看到复查字段", async ({ page }) => {
  const user = accounts.find((item: { username: string }) => item.username === "accept_admin");
  await page.goto("/actions");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "动作与复查", exact: true })).toBeVisible();

  const asinSelect = page.getByLabel("子 ASIN", { exact: true }).first();
  await asinSelect.click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").first().click();
  const title = `S5 草稿 ${Date.now()}`;
  await page.getByLabel("任务", { exact: true }).fill(title);
  await page.getByRole("button", { name: "建立任务", exact: true }).click();
  const actionTable = page.locator(".ant-table-wrapper").first();
  let row = actionTable.getByRole("row").filter({ hasText: title });
  for (let pageIndex = 0; pageIndex < 20 && (await row.count()) === 0; pageIndex += 1) {
    const next = actionTable.locator(".ant-table-pagination").getByRole("listitem", { name: "下一页" }).getByRole("button");
    if (await next.isDisabled()) break;
    await next.click();
    await page.waitForTimeout(150);
    row = actionTable.getByRole("row").filter({ hasText: title });
  }
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "修订草稿", exact: true }).click();
  const revisedTitle = `${title} 已修订`;
  await page.getByRole("dialog").getByLabel("任务标题", { exact: true }).fill(revisedTitle);
  await page.getByRole("dialog").getByRole("button", { name: "保存草稿", exact: true }).click();
  await expect(page.getByRole("row").filter({ hasText: revisedTitle })).toBeVisible();
});

test("S5 提案页面展示策略并记录外部人工交接", async ({ page }) => {
  const user = accounts.find((item: { username: string }) => item.username === "accept_admin");
  await page.goto("/actions");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByText("创建变更提案", { exact: true })).toBeVisible();
  const asin = page.getByLabel("子 ASIN", { exact: true }).last();
  await asin.click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").first().click();
  await page.getByLabel("提案类型", { exact: true }).click();
  await page.locator(".ant-select-dropdown:visible").getByText("价格变更", { exact: true }).click();
  await expect(page.getByText("价格、库存和 Listing 提案只记录人工经营变更授权", { exact: false })).toBeVisible();
  const proposalKey = `browser-price-${Date.now()}`;
  await page.getByLabel("提案编号", { exact: true }).fill(proposalKey);
  await page.getByLabel("结构化目标", { exact: true }).fill('{"field":"price","currency":"USD"}');
  await page.getByLabel("变更前值", { exact: true }).fill('{"value":"20.00"}');
  await page.getByLabel("变更后值", { exact: true }).fill('{"value":"18.00"}');
  await page.getByLabel("依据编号", { exact: true }).fill("snapshot:browser");
  await page.getByRole("button", { name: "保存变更提案", exact: true }).click();
  const row = page.getByRole("row").filter({ hasText: proposalKey });
  await expect(row).toBeVisible();
  await expect(row.getByText("需人工外部授权", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "查看并审核", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await dialog.getByLabel("审核决定", { exact: true }).click();
  await page.locator(".ant-select-dropdown:visible").getByText("批准", { exact: true }).click();
  await dialog.getByLabel("审核依据", { exact: true }).fill("已核对价格快照并确认人工执行边界");
  await dialog.getByRole("button", { name: "保存审核结果", exact: true }).click();
  await expect(row.getByText("已批准", { exact: true })).toBeVisible();
  await row.getByRole("button", { name: "查看并审核", exact: true }).click();
  const approvedDialog = page.getByRole("dialog");
  await approvedDialog.getByLabel("人工交接编号", { exact: true }).fill(`SC-${Date.now()}`);
  await approvedDialog.getByLabel("授权说明", { exact: true }).fill("由运营在 Seller Central 手工调整，系统不执行外部写入");
  await approvedDialog.getByRole("button", { name: "记录人工授权（不执行外部变更）", exact: true }).click();
  await expect(row.getByText("已交接", { exact: false })).toBeVisible();
});

test("S5 复查页面展示基线、变化、窗口、方法和成熟度", async ({ page }) => {
  const user = accounts.find((item: { username: string }) => item.username === "accept_admin");
  await page.goto("/actions");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option-content").getByText("测试店铺 A / US", { exact: true }).click();
  await page.waitForTimeout(800);
  const actionTable = page.locator(".ant-table-wrapper").first();
  let row = actionTable.getByRole("row").filter({ hasText: "S5 复查展示验收" });
  for (let pageIndex = 0; pageIndex < 20 && (await row.count()) === 0; pageIndex += 1) {
    const next = actionTable.locator(".ant-table-pagination").getByRole("listitem", { name: "下一页" }).getByRole("button");
    if (await next.isDisabled()) break;
    await next.click();
    await page.waitForTimeout(150);
    row = actionTable.getByRole("row").filter({ hasText: "S5 复查展示验收" });
  }
  await expect(row).toHaveCount(1);
  await expect(row).toContainText("变化 -0.04");
  await expect(row).toContainText("cohort_mature_queue_v1");
  await row.getByRole("button", { name: "安排复查", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("复查已完成", { exact: true })).toBeVisible();
  await expect(dialog.getByText("观察窗口 2026-09-10 至 2026-09-16", { exact: false })).toBeVisible();
  await expect(dialog.getByText("成熟度 ready", { exact: false })).toBeVisible();
});
