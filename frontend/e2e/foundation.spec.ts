import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const accounts: { username: string; password: string }[] = JSON.parse(
  readFileSync(
    new URL("../../.local/acceptance-accounts.json", import.meta.url),
    "utf8",
  ),
);

test("管理员登录、维护映射、真实队列执行并退出", async ({ page }) => {
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/products");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "商品映射", exact: true }),
  ).toBeVisible();
  await expect(page.getByText("验收测试数据", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "新增商品映射" }).click();
  await page.getByLabel("子 ASIN", { exact: true }).fill("B000TEST02");
  const sku = `E2E-${Date.now()}`;
  await page.getByLabel("SKU", { exact: true }).fill(sku);
  await page.getByLabel("商品名称", { exact: true }).fill("浏览器验收商品");
  await page.getByLabel("生效日期", { exact: true }).fill("2026-09-01");
  await page
    .getByLabel("变更原因", { exact: true })
    .fill("验证浏览器写入与历史追溯");
  await page.getByRole("button", { name: "保存映射" }).click();
  await expect(page.getByText(sku, { exact: true })).toBeVisible();
  await page.reload();
  const row = page.getByRole("row").filter({ hasText: sku });
  await row.getByRole("button", { name: "查看历史" }).click();
  await expect(
    page.getByText("变更原因：验证浏览器写入与历史追溯"),
  ).toBeVisible();
  await page.getByRole("button", { name: "关闭", exact: true }).click();
  await page.screenshot({
    path: "test-results/s1-products.png",
    fullPage: true,
  });
  await page.getByRole("menuitem", { name: "后台任务" }).click();
  await page.getByRole("button", { name: "检查后台服务" }).click();
  await expect(
    page.getByText("后台任务已执行，数据库连接正常。").first(),
  ).toBeVisible({ timeout: 30000 });
  await page.getByRole("button", { name: "退出" }).click();
  await expect(page.getByRole("heading", { name: "登录工作台" })).toBeVisible();
});

test("运营账号只看到授权店铺，不能维护映射", async ({ page }) => {
  const user = accounts.find((u) => u.username === "accept_operator_b")!;
  await page.goto("/products");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByText("测试店铺 B / US", { exact: true }),
  ).toBeVisible();
  await expect(page.getByRole("button", { name: "新增商品映射" })).toHaveCount(
    0,
  );
  await page.getByRole("combobox", { name: "选择店铺" }).click();
  await expect(page.getByText("测试店铺 A / US", { exact: true })).toHaveCount(
    0,
  );
});
