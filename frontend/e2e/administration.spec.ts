import { test, expect } from "@playwright/test";
import { accounts } from "./support/accounts";
import { randomBytes } from "node:crypto";

test("管理员登记店铺及普通成员并授予范围，成员登录后仅见该店铺", async ({
  page,
  browser,
}) => {
  test.setTimeout(60000);
  const admin = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/settings");
  await page.getByLabel("用户名", { exact: true }).fill(admin.username);
  await page.getByLabel("密码", { exact: true }).fill(admin.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "店铺与成员权限", exact: true }),
  ).toBeVisible();
  const suffix = Date.now();
  const shop = `验收 D：权限-${suffix}`;
  await page.getByRole("button", { name: "登记店铺", exact: true }).click();
  await page.getByLabel("店铺名称", { exact: true }).fill(shop);
  await page.getByRole("button", { name: "保存店铺", exact: true }).click();
  await expect(
    page.getByRole("cell", { name: shop, exact: true }),
  ).toBeVisible();
  const username = `e2e_member_${suffix}`,
    password = randomBytes(18).toString("base64url");
  await page.getByRole("button", { name: "新增成员账号", exact: true }).click();
  await page.getByLabel("用户名", { exact: true }).fill(username);
  await page.getByLabel("初始密码", { exact: true }).fill(password);
  await page.getByRole("button", { name: "创建成员", exact: true }).click();
  const member = page.getByRole("row").filter({ hasText: username });
  await expect(member).toBeVisible();
  await member
    .getByRole("button", { name: "配置店铺权限", exact: true })
    .click();
  await page.getByLabel("授权店铺", { exact: true }).click();
  await page.getByLabel("授权店铺", { exact: true }).fill(shop);
  await page
    .locator(".ant-select-item-option-content")
    .filter({ hasText: new RegExp(`^${shop} / US$`) })
    .click();
  await page.getByLabel("角色", { exact: true }).click();
  await page
    .locator(".ant-select-item-option-content")
    .filter({ hasText: /^运营$/ })
    .click();
  await page.keyboard.press("Escape");
  await page.getByRole("button", { name: "保存授权", exact: true }).click();
  await expect(member).toContainText(shop);
  await page
    .locator(".ant-drawer-content-wrapper")
    .evaluateAll((elements) =>
      Promise.all(
        elements.flatMap((e) =>
          e.getAnimations({ subtree: true }).map((a) => a.finished),
        ),
      ),
    );
  await page.screenshot({
    path: "test-results/s1-permissions.png",
    fullPage: true,
  });
  const context = await browser.newContext();
  const memberPage = await context.newPage();
  await memberPage.goto("http://127.0.0.1:5184/products");
  await memberPage.getByLabel("用户名", { exact: true }).fill(username);
  await memberPage.getByLabel("密码", { exact: true }).fill(password);
  await memberPage.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    memberPage.getByText(`${shop} / US`, { exact: true }),
  ).toBeVisible();
  await expect(
    memberPage.getByRole("button", { name: "新增商品映射" }),
  ).toHaveCount(0);
  await memberPage.getByLabel("选择店铺", { exact: true }).click();
  await expect(
    memberPage.getByText("测试店铺 A / US", { exact: true }),
  ).toHaveCount(0);
  await memberPage.keyboard.press("Escape");
  await member.getByRole("button", { name: "强制退出", exact: true }).click();
  await page
    .getByLabel("操作原因", { exact: true })
    .fill("验收：撤销旧设备会话");
  await page.getByRole("button", { name: "确认强制退出", exact: true }).click();
  await expect(page.locator(".ant-drawer-content-wrapper")).toBeHidden();
  await memberPage.reload();
  await expect(
    memberPage.getByRole("heading", { name: "登录工作台" }),
  ).toBeVisible();
  await memberPage.getByLabel("用户名", { exact: true }).fill(username);
  await memberPage.getByLabel("密码", { exact: true }).fill(password);
  await memberPage.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    memberPage.getByText(`${shop} / US`, { exact: true }),
  ).toBeVisible();
  const newPassword = randomBytes(18).toString("base64url");
  await member.getByRole("button", { name: "重置密码", exact: true }).click();
  await page.getByLabel("新密码", { exact: true }).fill(newPassword);
  await page.getByLabel("操作原因", { exact: true }).fill("验收：重置成员密码");
  await page
    .getByRole("button", { name: "确认重置并退出", exact: true })
    .click();
  await expect(page.locator(".ant-drawer-content-wrapper")).toBeHidden();
  await memberPage.reload();
  await expect(
    memberPage.getByRole("heading", { name: "登录工作台" }),
  ).toBeVisible();
  await memberPage.getByLabel("用户名", { exact: true }).fill(username);
  await memberPage.getByLabel("密码", { exact: true }).fill(password);
  await memberPage.getByRole("button", { name: "登录", exact: true }).click();
  await expect(memberPage.getByRole("alert")).toContainText(
    "用户名或密码不正确",
  );
  await memberPage.getByLabel("密码", { exact: true }).fill(newPassword);
  await memberPage.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    memberPage.getByText(`${shop} / US`, { exact: true }),
  ).toBeVisible();
  await context.close();
});
