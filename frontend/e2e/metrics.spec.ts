import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const accounts: { username: string; password: string }[] = JSON.parse(
  readFileSync(
    new URL("../../.local/acceptance-accounts.json", import.meta.url),
    "utf8",
  ),
);

test("运营扫描已发布事实，查看事件及分子分母", async ({ page }) => {
  test.setTimeout(90000);
  const user = accounts.find((u) => u.username === "accept_operator_a")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "事件队列", exact: true }),
  ).toBeVisible();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 C：指标与追溯 / US", { exact: true }).click();
  await page
    .getByRole("button", { name: "扫描已发布数据", exact: true })
    .click();
  await expect(page.getByText("扫描完成", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  await expect(
    page.getByRole("cell", { name: "销量下降", exact: true }),
  ).toBeVisible({ timeout: 10000 });
  await page.getByRole("button", { name: "查看计算依据", exact: true }).click();
  await expect(
    page.getByText("计算依据与数据范围", { exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "4.00%", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "28", exact: true }),
  ).toBeVisible();
  await expect(
    page.getByRole("cell", { name: "14", exact: true }),
  ).toBeVisible();
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
    path: "test-results/s3-metrics.png",
    fullPage: true,
  });
});

test("管理员调整每日扫描时间，刷新后保留", async ({ page }) => {
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 C：指标与追溯 / US", { exact: true }).click();
  await page.getByRole("button", { name: "设置扫描时间", exact: true }).click();
  await page.getByLabel("每日扫描时间", { exact: true }).fill("08:05");
  await page.getByRole("button", { name: "保存扫描计划", exact: true }).click();
  await expect(
    page.getByText("每日扫描：08:05（Asia/Shanghai）", { exact: true }),
  ).toBeVisible();
  await page.reload();
  await expect(page.getByText("验收 C：指标与追溯 / US", { exact: true })).toBeVisible();
  await expect(
    page.getByText("每日扫描：08:05（Asia/Shanghai）", { exact: true }),
  ).toBeVisible();
  await page.getByRole("button", { name: "设置扫描时间", exact: true }).click();
  await page.getByLabel("每日扫描时间", { exact: true }).fill("08:00");
  await page.getByRole("button", { name: "保存扫描计划", exact: true }).click();
  await expect(
    page.getByText("每日扫描：08:00（Asia/Shanghai）", { exact: true }),
  ).toBeVisible();
});

test("管理员预览阈值并保存版本，恢复验收默认值", async ({ page }) => {
  test.setTimeout(90000);
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 C：指标与追溯 / US", { exact: true }).click();
  for (const threshold of [99, 30]) {
    await page
      .getByRole("button", { name: "调整检测阈值", exact: true })
      .click();
    await page
      .getByLabel("销量相对变化（%）", { exact: true })
      .fill(String(threshold));
    await page
      .getByRole("button", { name: "预览全店影响", exact: true })
      .click();
    await expect(
      page.getByRole("heading", { name: "命中信号变化", exact: true }),
    ).toBeVisible({ timeout: 30000 });
    await page
      .getByLabel("阈值修改原因", { exact: true })
      .fill(
        threshold === 99 ? "浏览器验收预览规则变化" : "恢复验收默认检测规则",
      );
    await page
      .getByRole("button", { name: "保存本店阈值并重扫", exact: true })
      .click();
    await expect(
      page.getByText("检测阈值与影响预览", { exact: true }),
    ).not.toBeVisible();
  }
});

test("管理员重算历史日期并查看独立快照", async ({ page }) => {
  test.setTimeout(90000);
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 C：指标与追溯 / US", { exact: true }).click();
  const yesterday = new Date(Date.now() - 86400000);
  const targetDate = new Intl.DateTimeFormat("en-CA", {
    timeZone: "America/Los_Angeles",
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
  }).format(yesterday);
  await page.getByLabel("历史重算日期", { exact: true }).fill(targetDate);
  await page.getByRole("button", { name: "重算历史日期", exact: true }).click();
  await expect(page.getByText("扫描完成", { exact: true })).toBeVisible({
    timeout: 30000,
  });
  await page.getByRole("button", { name: "查看计算依据", exact: true }).click();
  await expect(
    page.getByText(
      `历史重算 · 分析日期 ${targetDate} · 使用重算时已发布数据与配置`,
      { exact: true },
    ),
  ).toBeVisible();
  await page
    .getByRole("heading", { name: "退款与退货发生量", exact: true })
    .scrollIntoViewIfNeeded();
  await page
    .locator(".ant-drawer-content-wrapper")
    .evaluateAll((elements) =>
      Promise.all(
        elements.flatMap((element) =>
          element
            .getAnimations({ subtree: true })
            .map((animation) => animation.finished),
        ),
      ),
    );
  await page.screenshot({
    path: "test-results/s3-details.png",
    fullPage: true,
  });
});

test("管理员回补历史区间并选择日期追溯", async ({ page }) => {
  test.setTimeout(90000);
  const user = accounts.find((u) => u.username === "accept_admin")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 C：指标与追溯 / US", { exact: true }).click();
  const formatter = new Intl.DateTimeFormat("en-CA", { timeZone: "America/Los_Angeles", year: "numeric", month: "2-digit", day: "2-digit" });
  const start = formatter.format(new Date(Date.now() - 3 * 86400000));
  const end = formatter.format(new Date(Date.now() - 2 * 86400000));
  await page.getByLabel("历史回补开始日期", { exact: true }).fill(start);
  await page.getByLabel("历史回补结束日期", { exact: true }).fill(end);
  await page.getByRole("button", { name: "回补历史区间", exact: true }).click();
  await expect(page.getByText("历史区间回补完成", { exact: true })).toBeVisible({ timeout: 40000 });
  await page.getByLabel("查看回补日期", { exact: true }).click();
  await page.locator(".ant-select-item-option-content").filter({ hasText: start }).click();
  await expect(page.getByText(`历史重算 · 分析日期 ${start} · 使用重算时已发布数据与配置`, { exact: true })).toBeVisible();
  await expect(page.getByRole("heading", { name: "实际采集评论均分", exact: true })).toBeVisible();
  await expect(page.getByText("采集样本口径", { exact: true })).toBeVisible();
});
