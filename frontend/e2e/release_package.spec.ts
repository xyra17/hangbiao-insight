import { expect, test } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { accounts } from "./support/accounts";

const folder = process.env.RELEASE_PACKAGE_OUTPUT;
test("从全量经营包导入对象核对原文件、指标、来源、调查、报告和动作", async ({ page }) => {
  test.skip(!folder, "requires explicit full imported package run; ordinary seed is insufficient");
  test.setTimeout(180000);
  const identity = JSON.parse(readFileSync(resolve(folder!, "identities.json"), "utf8"));
  const state = JSON.parse(readFileSync(resolve(folder!, "state.json"), "utf8"));
  const snapshots = JSON.parse(readFileSync(resolve(folder!, "snapshots-and-oracle.json"), "utf8"));
  const user = accounts.find((a) => a.username === "accept_admin")!;
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  const loginPromise = page.waitForResponse((r) => r.url().endsWith("/api/v1/login/") && r.request().method() === "POST");
  await page.getByRole("button", { name: "登录", exact: true }).click();
  expect((await loginPromise).status()).toBe(200);
  await page.getByLabel("选择店铺", { exact: true }).click();
  const dropdown = page.locator(".ant-select-dropdown:visible");
  const option = dropdown.getByText(`${identity.shop_name} / US`, { exact: true });
  if (!(await option.isVisible())) {
    const holder = dropdown.locator(".ant-select-dropdown-list-holder");
    await holder.waitFor({ timeout: 10000 });
    await holder.evaluate((el) => { el.scrollTop = el.scrollHeight; });
  }
  await option.click({ timeout: 10000 });
  const sourcesResponse = await page.request.get(`/api/v1/sources/?shop=${identity.shop_id}&page_size=100`);
  expect(sourcesResponse.ok()).toBeTruthy();
  const sources = (await sourcesResponse.json()).results;
  const mainSourceIds = new Set(Object.values(state.sources));
  expect(sources.filter((s: { id: string; is_primary: boolean }) => mainSourceIds.has(s.id) && s.is_primary)).toHaveLength(identity.pack === "04" ? 8 : 9);
  expect(sources.filter((s: { id: string; is_primary: boolean; kind: string }) => s.is_primary && !mainSourceIds.has(s.id)).every((s: { kind: string }) => identity.pack === "04" && s.kind === "reviews")).toBeTruthy();
  const salesBatch = Object.values(state.files).find((item: any) => item.source_id === state.sources.sales_traffic) as any;
  const batch = await page.request.get(`/api/v1/imports/${salesBatch.batch_id}/`);
  expect((await batch.json()).valid_rows).toBe(identity.pack === "04" ? 5952 : 1200);
  const original = await page.request.get(`/api/v1/imports/${salesBatch.batch_id}/original-file/`);
  expect(original.status()).toBe(200);
  for (const sample of snapshots) {
    const response = await page.request.get(`/api/v1/snapshots/${sample.snapshot.id}/`);
    expect(response.ok()).toBeTruthy();
    const actual = await response.json();
    expect(actual.local_date).toBe(state.as_of);
    if (identity.pack === "04") expect(actual.payload.daily.status).toBe("missing");
    else {
      expect(actual.payload.daily.current.ordered_units).toBe(Number(sample.independent_daily_oracle.current.ordered_units));
      expect(actual.payload.daily.current.sessions).toBe(Number(sample.independent_daily_oracle.current.sessions));
      expect(actual.payload.daily.baseline.ordered_units).toBe(Number(sample.independent_daily_oracle.baseline.ordered_units));
    }
  }
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const route of ["sources", "events", "reports", "actions", "investigations"]) {
      await page.goto(`/${route}`);
      await expect(page.getByRole("banner").getByText(`${identity.shop_name} / US`, { exact: true })).toBeVisible();
      await expect(page.locator("main")).toBeVisible();
      await expect(page.locator(".ant-spin-spinning")).toHaveCount(0);
      if (route === "sources") {
        const salesSource = sources.find((source: { id: string }) => source.id === state.sources.sales_traffic);
        await page.getByLabel("选择数据源", { exact: true }).click();
        const sourceDropdown = page.locator(".ant-select-dropdown:visible");
        const salesOption = sourceDropdown.getByText(salesSource.name, { exact: true });
        for (let attempt = 0; attempt < 20 && !(await salesOption.isVisible()); attempt++) {
          await sourceDropdown.locator(".ant-select-dropdown-list-holder").evaluate((el) => { el.scrollTop += 250; });
        }
        await salesOption.click();
        await expect(sourceDropdown).toBeHidden();
        const filename = Object.entries(state.files).find(([, item]: any) => item.batch_id === salesBatch.batch_id)![0].split("/").pop()!;
        const importedRow = page.getByRole("row").filter({ hasText: filename });
        await expect(importedRow).toBeVisible();
        await expect(importedRow).toContainText(identity.pack === "04" ? "5952 / 0" : "1200 / 0");
        await expect(importedRow).toContainText("已发布");
      }
      expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width + 1);
      await page.evaluate(() => window.scrollTo(0, 0));
      await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
      await page.screenshot({ path: resolve(folder!, "screenshots", `${width}-${identity.pack}-${route}.png`), fullPage: true });
    }
  }
  if (identity.pack === "05") {
    await page.goto("/actions");
    await expect(page.getByText("[SYNTHETIC] release improved_after_action", { exact: true })).toBeVisible();
    await expect(page.getByText("[SYNTHETIC] release no_improvement", { exact: true })).toBeVisible();
    const actions = JSON.parse(readFileSync(resolve(folder!, "actions-state.json"), "utf8"));
    for (const scenario of actions.scenarios) {
      const row = page.getByRole("row").filter({ hasText: `[SYNTHETIC] release ${scenario.scenario_id}` });
      await expect(row).toBeVisible();
      if (scenario.oracle) {
        await expect(row).toContainText(`基线总量：${Number(scenario.oracle.baseline_total).toLocaleString("zh-CN")} 件`);
        await expect(row).toContainText(`观察窗口总量：${Number(scenario.oracle.current_total).toLocaleString("zh-CN")} 件`);
        await expect(row).toContainText(`基线窗口：${scenario.oracle.baseline_start} 至 ${scenario.oracle.baseline_end}`);
        await expect(row).toContainText(`观察窗口：${scenario.oracle.current_start} 至 ${scenario.oracle.current_end}`);
        await row.getByText("查看来源与追溯", { exact: true }).first().click();
        await expect(row).toContainText("来源版本：");
        await expect(row).toContainText(scenario.snapshot_id);
      } else {
        await expect(row).toContainText("scheduled");
        await expect(row.locator(".wb-review-summary")).toHaveCount(0);
      }
    }
    await page.evaluate(() => window.scrollTo(0, 0));
    await expect.poll(() => page.evaluate(() => window.scrollY)).toBe(0);
    await page.screenshot({ path: resolve(folder!, "screenshots", "1440-05-actions-verified.png"), fullPage: true });

    const forward = JSON.parse(readFileSync(resolve(folder!, "representative-lineage.json"), "utf8"));
    const clockShop = identity.forward_action_case;
    await page.getByLabel("选择店铺", { exact: true }).click();
    const shopDropdown = page.locator(".ant-select-dropdown:visible");
    await shopDropdown.getByText(`${clockShop.shop_name} / US`, { exact: true }).click();
    const forwardRow = page.getByRole("row").filter({ hasText: forward.task_title });
    await expect(forwardRow).toBeVisible();
    await expect(forwardRow).toContainText(forward.report_id.slice(0, 8));
    await expect(forwardRow).toContainText(forward.investigation_id.slice(0, 8));
    await expect(forwardRow).toContainText("人工已确认");
    const forwardTask = await page.request.get(`/api/v1/action-tasks/${forward.task_id}/`);
    expect(forwardTask.ok()).toBeTruthy();
    const forwardTaskJson = await forwardTask.json();
    expect(forwardTaskJson.source_report).toBe(forward.report_id);
    expect(forwardTaskJson.source_investigation).toBe(forward.investigation_id);
    expect(forwardTaskJson.source_lineage.metric_snapshot).toBe(forward.baseline_snapshot_id);
    expect(forwardTaskJson.review_comparison.ordered_units.window_start).toBe("2026-09-24");
    expect(forwardTaskJson.review_comparison.ordered_units.window_end).toBe("2026-09-30");
    await page.screenshot({ path: resolve(folder!, "screenshots", "1440-05-forward-action-lineage.png"), fullPage: true });

    const complete = JSON.parse(readFileSync(resolve(folder!, "representative-lineage-complete.json"), "utf8"));
    const completeResult = JSON.parse(readFileSync(resolve(folder!, "forward-complete-result.json"), "utf8"));
    expect(completeResult.status).toBe("passed");
    expect(completeResult.report_status).toBe("partial");
    expect(complete.pre_action_daily_status).toBe("ready");
    expect(complete.investigation_completion_status).toBe("facts_available");
    expect(complete.report_claim_scope).toContain("not full business-domain report completeness");
    expect(complete.requested_metric_subtasks).toEqual([
      expect.objectContaining({ family: "daily", mode: "daily", status: "facts_available" }),
    ]);
    const completeShop = identity.forward_complete_action_case;
    await page.getByLabel("选择店铺", { exact: true }).click();
    const completeShopDropdown = page.locator(".ant-select-dropdown:visible");
    const completeShopOption = completeShopDropdown.getByText(`${completeShop.shop_name} / US`, { exact: true });
    if (!(await completeShopOption.isVisible())) {
      const holder = completeShopDropdown.locator(".ant-select-dropdown-list-holder");
      await holder.waitFor({ timeout: 10000 });
      await holder.evaluate((el) => { el.scrollTop = el.scrollHeight; });
    }
    await completeShopOption.click({ timeout: 10000 });
    const completeRow = page.getByRole("row").filter({ hasText: complete.task_title });
    await expect(completeRow).toBeVisible();
    await expect(completeRow).toContainText(complete.report_id.slice(0, 8));
    await expect(completeRow).toContainText(complete.investigation_id.slice(0, 8));
    await expect(completeRow).toContainText("人工已确认");
    const completeTask = await page.request.get(`/api/v1/action-tasks/${complete.task_id}/`);
    expect(completeTask.ok()).toBeTruthy();
    const completeTaskJson = await completeTask.json();
    expect(completeTaskJson.source_report).toBe(complete.report_id);
    expect(completeTaskJson.source_investigation).toBe(complete.investigation_id);
    expect(completeTaskJson.source_lineage.metric_snapshot).toBe(complete.baseline_snapshot_id);
    expect(completeTaskJson.review_comparison.ordered_units.window_start).toBe("2026-09-25");
    expect(completeTaskJson.review_comparison.ordered_units.window_end).toBe("2026-10-01");
    const actionTable = page.locator(".wb-action-task-table .ant-table-content");
    await expect.poll(() => actionTable.evaluate((el) => el.scrollWidth > el.clientWidth)).toBeTruthy();
    await page.screenshot({ path: resolve(folder!, "screenshots", "1440-05-forward-action-lineage-complete.png"), fullPage: true });
    await page.setViewportSize({ width: 390, height: 844 });
    const mobileMenu = page.getByRole("button", { name: "切换导航菜单" });
    await expect(mobileMenu).toHaveAttribute("aria-expanded", "false");
    await expect.poll(() => page.locator(".wb-sidebar").evaluate((el) => el.getBoundingClientRect().width)).toBeLessThanOrEqual(1);
    const mobileMenuBox = await mobileMenu.boundingBox();
    const shopSelectBox = await page.locator(".wb-shop-select").boundingBox();
    expect(mobileMenuBox && shopSelectBox).toBeTruthy();
    expect(mobileMenuBox!.x + mobileMenuBox!.width).toBeLessThanOrEqual(shopSelectBox!.x);
    await expect(page.getByRole("heading", { name: "动作与复查", exact: true })).toBeVisible();
    await expect(completeRow).toBeVisible();
    await page.screenshot({ path: resolve(folder!, "screenshots", "390-05-forward-action-lineage-complete.png"), fullPage: true });

    const mobileActionTable = page.locator(".wb-action-task-table .ant-table-content");
    await expect.poll(() => mobileActionTable.evaluate((el) => el.scrollWidth > el.clientWidth)).toBeTruthy();
    await mobileActionTable.evaluate((el) => { el.scrollLeft = el.scrollWidth; });
    await completeRow.getByRole("button", { name: "复查详情", exact: true }).click();
    const reviewDialog = page.getByRole("dialog");
    await expect(reviewDialog.getByText("前后指标对比", { exact: true })).toBeVisible();
    const trace = reviewDialog.locator(".wb-review-trace");
    await trace.getByText("查看来源与追溯", { exact: true }).click();
    await expect(trace.getByText("来源版本：")).toBeVisible();
    await expect(trace.getByText(complete.review_snapshot_id)).toBeVisible();
    await page.screenshot({
      path: resolve(folder!, "screenshots", "390-05-forward-action-review-trace.png"),
      animations: "disabled",
    });
  }
});
