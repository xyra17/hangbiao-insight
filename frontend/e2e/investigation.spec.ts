import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";

const accounts: { username: string; password: string }[] = JSON.parse(
  readFileSync(
    new URL("../../.local/acceptance-accounts.json", import.meta.url),
    "utf8",
  ),
);

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
