import { test, expect } from "@playwright/test";
import { accounts } from "./support/accounts";


test("自主调查明确区分内部草稿权限和外部经营变更边界", async ({ page }) => {
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "自主调查", exact: true })).toBeVisible();
  await expect(
    page.getByText("不会执行 Amazon 或 ERP 的外部经营变更", { exact: false }),
  ).toBeVisible();
  const permission = page.getByLabel("允许调查完成后创建内部核查草稿", { exact: true });
  await expect(permission).toBeVisible();
  await expect(permission).not.toBeChecked();
  await permission.check();
  await expect(permission).toBeChecked();
});

test("授权范围内调查展示预算、决策和工具回执", async ({ page }) => {
  test.setTimeout(60000);
  const user = accounts.find((item) => item.username === "accept_admin")!;
  await page.goto("/investigations");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(page.getByRole("heading", { name: "自主调查", exact: true })).toBeVisible();
  await page
    .getByLabel("问题", { exact: true })
    .fill("请调查 B000TEST01 的退货评论原因");
  await page.getByRole("button", { name: "开始调查", exact: true }).click();
  await expect(page.getByText("后台任务：completed", { exact: false })).toBeVisible({
    timeout: 30000,
  });
  await expect(page.getByText("调查工具权限矩阵", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  await expect(page.getByText(/调查预算：剩余/)).toBeVisible();
  await expect(page.getByText("metric_snapshot", { exact: true })).toBeVisible();
  await expect(
    page.getByText("不会执行 Amazon 或 ERP 的外部经营变更", { exact: false }),
  ).toBeVisible();
});
