import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const accounts: { username: string; password: string }[] = JSON.parse(
  readFileSync(
    new URL("../../.local/acceptance-accounts.json", import.meta.url),
    "utf8",
  ),
);

test("报告与事件能从工作台下载，保留版本与数据范围", async ({ page }) => {
  test.setTimeout(60000);
  const user = accounts.find((account) => account.username === "accept_admin")!;
  await page.goto("/reports");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await expect(
    page.getByRole("heading", { name: "调查报告", exact: true }),
  ).toBeVisible();
  await page.getByLabel("子 ASIN", { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible .ant-select-item-option")
    .first()
    .click();
  await page.getByRole("button", { name: "开始取证", exact: true }).click();
  await expect(
    page.getByRole("button", { name: "查看刚生成的报告", exact: true }),
  ).toBeVisible({ timeout: 30000 });
  await page
    .getByRole("button", { name: "查看刚生成的报告", exact: true })
    .click();
  for (const extension of ["HTML", "JSON"]) {
    const pending = page.waitForEvent("download");
    await page
      .getByRole("link", { name: `下载报告 ${extension}`, exact: true })
      .click();
    const download = await pending;
    expect(download.suggestedFilename()).toMatch(
      new RegExp(`-v\\d+\\.${extension.toLowerCase()}$`),
    );
    const stream = await download.createReadStream();
    const chunks: Buffer[] = [];
    for await (const chunk of stream!) chunks.push(Buffer.from(chunk));
    const text = Buffer.concat(chunks).toString("utf8");
    if (extension === "JSON") {
      const doc = JSON.parse(text);
      expect(doc.report_version).toBeGreaterThan(0);
      expect(doc).toHaveProperty("metric_source_manifest");
      expect(doc.payload.limitations.length).toBeGreaterThan(0);
    } else {
      expect(text).toContain("限制与待补数据");
    }
  }
  await page.goto("/events");
  await expect(
    page.getByRole("heading", { name: "事件队列", exact: true }),
  ).toBeVisible();
  const pending = page.waitForEvent("download");
  await page
    .getByRole("link", { name: "导出当前筛选 CSV", exact: true })
    .click();
  expect((await pending).suggestedFilename()).toBe("events.csv");
});
