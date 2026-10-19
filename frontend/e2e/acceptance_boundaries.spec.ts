import { expect, test, type Page } from "@playwright/test";
import { accounts } from "./support/accounts";

async function loginAsAdmin(page: Page) {
  const admin = accounts.find((item) => item.username === "accept_admin");
  expect(admin, "acceptance administrator credentials are required").toBeTruthy();
  await page.goto("/actions");
  await page.getByLabel("用户名", { exact: true }).fill(admin!.username);
  await page.getByLabel("密码", { exact: true }).fill(admin!.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
}

async function chooseShop(page: Page, name: string) {
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
    .getByText(name, { exact: true })
    .click();
}

const localDateTime = (milliseconds: number) => {
  const date = new Date(milliseconds);
  return new Date(date.getTime() - date.getTimezoneOffset() * 60_000)
    .toISOString()
    .slice(0, 16);
};

const previousDate = (value: string) => {
  const date = new Date(`${value}T12:00:00Z`);
  date.setUTCDate(date.getUTCDate() - 1);
  return date.toISOString().slice(0, 10);
};

test("真实 API 拒绝动作复查中的过期来源和缺报窗口", async ({ page }) => {
  await loginAsAdmin(page);
  await chooseShop(page, "测试店铺 A / US");
  await expect(page.getByRole("heading", { name: "动作与复查", exact: true })).toBeVisible();

  const row = page
    .getByRole("row")
    .filter({ hasText: "S5 缺报与过期快照拒绝验收" });
  await expect(row).toHaveCount(1);
  await row.getByRole("button", { name: "安排复查", exact: true }).click();
  const dialog = page.getByRole("dialog");
  await expect(dialog.getByText("安排或完成复查", { exact: true })).toBeVisible();
  await dialog.getByLabel("复查状态", { exact: true }).click();
  await page.locator(".ant-select-dropdown:visible").getByText("已完成", { exact: true }).click();
  await dialog.getByLabel("复查时间", { exact: true }).fill(localDateTime(Date.now() - 3_600_000));
  await dialog.getByLabel("复查结论", { exact: true }).fill("真实 API 浏览器边界验收，不产生业务结论。");

  const asOf = process.env.ACCEPTANCE_AS_OF;
  const snapshotDropdown = page.locator(".ant-select-dropdown:visible");
  const staleOption = asOf
    ? snapshotDropdown.getByText(
        `${previousDate(asOf)} · acceptance-stale-source-fixture-v1`,
        { exact: true },
      )
    : snapshotDropdown
        .locator(".ant-select-item-option-content")
        .filter({ hasText: "acceptance-stale-source-fixture-v1" })
        .first();
  await dialog.getByLabel("复查指标快照", { exact: true }).click();
  await expect(staleOption).toBeVisible();
  await staleOption.click();
  const staleResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/action-tasks/") &&
    response.url().endsWith("/review/") &&
    response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: /保存复查$/ }).click();
  expect((await staleResponse).status()).toBe(400);
  await expect(dialog.getByRole("alert")).toContainText("需引用同范围的当前指标快照完成复查。");

  await dialog.getByLabel("复查指标快照", { exact: true }).click();
  const currentOption = asOf
    ? snapshotDropdown.getByText(
        `${asOf} · acceptance-review-current-no-coverage-v1`,
        { exact: true },
      )
    : snapshotDropdown
        .locator(".ant-select-item-option-content")
        .filter({ hasText: "acceptance-review-current-no-coverage-v1" })
        .first();
  await expect(currentOption).toBeVisible();
  await currentOption.click();
  await expect(dialog.locator("button[type=submit].ant-btn-loading")).toHaveCount(0);
  const missingResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/action-tasks/") &&
    response.url().endsWith("/review/") &&
    response.request().method() === "POST",
  );
  await dialog.getByRole("button", { name: /保存复查$/ }).click();
  expect((await missingResponse).status()).toBe(400);
  await expect(dialog.getByRole("alert")).toContainText("动作复查的基线或七日观察窗口存在缺报日期。");
  await expect(row).toContainText("scheduled");
});

test("报告刷新失败后通过工作台重试并退出失败状态", async ({ page }) => {
  await loginAsAdmin(page);
  await chooseShop(page, "验收 C：指标与追溯 / US");
  await page.goto("/reports");
  const reportRow = page
    .getByRole("row")
    .filter({ hasText: "B000TEST03" })
    .filter({ has: page.getByRole("cell", { name: "1", exact: true }) });
  await expect(reportRow).toHaveCount(1);
  await reportRow.getByRole("button", { name: "查看证据", exact: true }).click();
  await expect(page.getByText("数据修订：刷新失败", { exact: true })).toBeVisible();
  const retryButton = page.getByRole("button", { name: "重新尝试刷新", exact: true });
  await expect(retryButton).toBeVisible();
  const retryResponse = page.waitForResponse((response) =>
    response.url().includes("/api/v1/jobs/") &&
    response.url().endsWith("/retry-refresh/") &&
    response.request().method() === "POST",
  );
  await retryButton.click();
  expect((await retryResponse).status()).toBe(202);
  await expect(retryButton).toHaveCount(0, { timeout: 15000 });
  await expect(page.getByText(/数据修订：(等待刷新|刷新完成|需要重新确认)/).first()).toBeVisible();
});
