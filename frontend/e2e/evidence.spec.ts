import { test, expect } from "@playwright/test";
import { accounts } from "./support/accounts";

test("授权用户通过报告页查询原文并看到真实空态", async ({ page }) => {
  const user = accounts.find((u: {username:string}) => u.username === "accept_admin");
  await page.goto("/reports");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "调查报告", exact: true })).toBeVisible();
  await page.getByLabel("查询商品", { exact: true }).click();
  await page.locator(".ant-select-dropdown:visible .ant-select-item-option").first().click();
  await page.getByLabel("开始日期", { exact: true }).fill("2026-09-01");
  await page.getByLabel("结束日期", { exact: true }).fill("2026-09-23");
  await page.getByLabel("问题词或原文短语").fill("未命中的独特检索样本XYZ2026");
  const response = page.waitForResponse(r => r.url().endsWith("/api/v1/evidence-search/") && r.request().method() === "POST");
  await page.getByRole("button", { name: "查询证据", exact: true }).click();
  expect((await response).status()).toBe(200);
  await expect(page.getByText("当前范围没有命中；这不代表该问题不存在，可调整关键词或核对数据覆盖。")).toBeVisible();
  await expect(page.getByText(/检索记录：/)).toBeVisible();
  await page.screenshot({path:"test-results/s4-evidence-search.png", fullPage:true});
});
