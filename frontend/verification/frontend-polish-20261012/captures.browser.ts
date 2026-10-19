import { expect, test } from "@playwright/test";
import { rig, startInvestigation } from "./fixtures";
import { writeFileSync } from "node:fs";
const stage = process.env.VISUAL_STAGE || "after";
for (const width of [1440, 768, 390])
  test(`${stage} captures ${width}`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const state = await rig(page, false);
    const capture = async (name: string) => {
      await page.screenshot({
        path: `verification/frontend-polish-20261012/${stage}/${width}-${name}.png`,
        fullPage: true,
      });
      if (stage === "after")
        expect(
          await page.evaluate(() => document.documentElement.scrollWidth),
        ).toBeLessThanOrEqual(width + 1);
    };
    await page.goto("/investigations");
    await expect(
      page.getByRole("heading", { name: "登录工作台" }),
    ).toBeVisible();
    await capture("login");
    await page.getByLabel("用户名", { exact: true }).fill("controlled_preview");
    await page.getByLabel("密码", { exact: true }).fill("fixture-only");
    await page.getByRole("button", { name: "登录", exact: true }).click();
    await expect(page.getByRole("heading", { name: "自主调查" })).toBeVisible();
    await capture("investigation-form");
    await startInvestigation(page);
    await capture("investigation-result");
    await page.goto("/reports");
    await expect(page.getByRole("heading", { name: "调查报告" })).toBeVisible();
    await expect(
      page.locator(".ant-table-tbody tr.ant-table-row").first(),
    ).toBeVisible();
    await capture("reports");
    await page
      .getByRole("button", { name: "查看证据", exact: true })
      .first()
      .click();
    await expect(
      page.getByText("缺少可比退款来源，暂不能直接归因。"),
    ).toBeVisible();
    await capture("report-result");
    await page.getByLabel("查询商品", { exact: true }).click();
    await page
      .locator(".ant-select-dropdown:visible .ant-select-item-option-content")
      .getByText("B000TEST01", { exact: true })
      .click();
    await page.getByLabel("开始日期", { exact: true }).fill("2026-10-01");
    await page.getByLabel("结束日期", { exact: true }).fill("2026-10-12");
    await page.getByLabel("问题词或原文短语", { exact: true }).fill("包装");
    await page.getByRole("button", { name: "查询证据", exact: true }).click();
    await expect(
      page.getByText("检索未完成，不能据此判断没有证据"),
    ).toBeVisible();
    await capture("evidence");
    state.failReports = true;
    await page.reload();
    await expect(page.getByRole("heading", { name: "调查报告" })).toBeVisible();
    await page.waitForTimeout(500);
    await capture("reports-error");
    state.failReports = false;
    await page.goto("/jobs");
    await expect(page.getByRole("heading", { name: "后台任务" })).toBeVisible();
    await expect(
      page.locator(".ant-table-tbody tr.ant-table-row").first(),
    ).toBeVisible();
    await capture("jobs");
    writeFileSync(
      `verification/frontend-polish-20261012/${stage}/${width}-browser-log.json`,
      JSON.stringify(
        {
          requests: state.requests,
          pageerrors: state.errors,
          console: state.console,
        },
        null,
        2,
      ),
    );
    expect(state.errors).toEqual([]);
  });
