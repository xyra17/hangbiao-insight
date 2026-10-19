import { test, expect } from "@playwright/test";
import { accounts } from "./support/accounts";


test("登记来源、核对错误行、明确部分发布并保留追溯", async ({ page }) => {
  test.setTimeout(90000);
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/sources");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "数据源", exact: true }),
  ).toBeVisible();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
    .getByText("测试店铺 A / US", { exact: true })
    .click();
  await page.getByRole("button", { name: "登记数据源", exact: true }).click();
  await page
    .getByLabel("来源名称", { exact: true })
    .fill(`浏览器导入验收-${Date.now()}`);
  await page.getByLabel("设为此店铺该数据域的主来源").uncheck();
  await page.getByRole("button", { name: "保存来源契约" }).click();
  await expect(page.getByRole("heading", { name: "上传报表" })).toBeVisible();
  // Wait for drawer closing and focus restoration before filling the upload form.
  await expect(page.locator(".ant-drawer-content-wrapper")).toBeHidden();
  const raw =
    "child_asin,seller_sku,fulfillment,business_date,ordered_units,order_count,sessions,buybox_percent,sales_amount,currency\nB000TEST01,,,2026-09-01,10,8,100,,,\nB000TEST01,,,2026-09-02,invalid,8,100,,,\n";
  await page.getByLabel("CSV 文件", { exact: true }).setInputFiles({
    name: "acceptance-sales.csv",
    mimeType: "text/csv",
    buffer: Buffer.from(raw),
  });
  await page
    .getByLabel("报表生成时间（本机时区）", { exact: true })
    .fill("2026-09-03T10:00");
  await page.getByLabel("覆盖起始日", { exact: true }).fill("2026-09-01");
  await page.getByLabel("覆盖结束日", { exact: true }).fill("2026-09-02");
  await page
    .getByLabel("报表覆盖的子 ASIN", { exact: true })
    .fill("B000TEST01");
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
    .filter({ hasText: /^B000TEST01$/ })
    .click();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "上传并解析" }).click();
  await expect(page.getByText("导入核对与追溯", { exact: true })).toBeVisible({
    timeout: 20000,
  });
  await expect(
    page.getByText("存在错误行，完整性不能标为正常。", { exact: true }),
  ).toBeVisible({ timeout: 30000 });
  await expect(
    page.getByRole("button", { name: "确认发布", exact: true }),
  ).toBeDisabled();
  await page.getByLabel("已核对来源字段、粒度和覆盖范围").check();
  await page.getByLabel("确认仅发布有效行，并标记为部分数据").check();
  await page.getByRole("button", { name: "确认发布", exact: true }).click();
  await expect(page.getByText("发布任务：已完成", { exact: true })).toBeVisible(
    { timeout: 30000 },
  );
  await expect(
    page.getByText("ordered_units 须为非负整数", { exact: true }),
  ).toBeVisible();
  await page
    .getByRole("button", { name: "撤销当前发布影响", exact: true })
    .last()
    .click();
  const revokeDialog = page.getByRole("dialog");
  await revokeDialog
    .locator("textarea")
    .fill("来源方重新生成报表，撤回当前批次影响并保留审计占位");
  await revokeDialog.getByRole("button", { name: "提交纠错", exact: true }).click();
  await expect(
    page.getByText("已撤销至修订", { exact: false }),
  ).toBeVisible({ timeout: 30000 });
  await page.screenshot({ path: "test-results/s2-import.png", fullPage: true });
});
