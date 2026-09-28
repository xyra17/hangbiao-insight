import { test, expect } from "@playwright/test";
import { readFileSync } from "node:fs";
const accounts = JSON.parse(
  readFileSync(
    new URL("../../.local/acceptance-accounts.json", import.meta.url),
    "utf8",
  ),
);
const fixture = JSON.parse(
  readFileSync(
    new URL("../../.local/workflow-acceptance.json", import.meta.url),
    "utf8",
  ),
);

test("事件领取、关联动作执行、进入观察和有依据关闭", async ({ page }) => {
  test.setTimeout(90000);
  page.setDefaultTimeout(15000);
  const user = accounts.find(
    (a: { username: string }) => a.username === "accept_admin",
  );
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 D：事件处理闭环 / US", { exact: true }).click();
  const row = page
    .getByRole("row")
    .filter({ hasText: fixture.asin })
    .filter({ hasText: "合成流程验收事件" });
  await row.getByRole("button", { name: "查看证据", exact: true }).click();
  const panel = page
    .locator(".ant-drawer-body")
    .filter({ has: page.getByText("处理事件", { exact: false }) });
  const select = async (label: string, option: string) => {
    await panel.getByLabel(label, { exact: true }).click();
    await page
      .locator(".ant-select-dropdown:visible")
      .getByText(option, { exact: true })
      .click();
  };
  await select("处理操作", "领取或重新分派");
  await panel
    .getByLabel("处理依据", { exact: true })
    .fill("运营接手合成样本调查");
  await panel.getByRole("button", { name: "提交处理", exact: true }).click();
  await expect(
    panel.getByRole("heading", { name: "处理事件 · 处理中", exact: true }),
  ).toBeVisible();
  const title = `核查包装-${fixture.asin}`;
  await panel.getByLabel("核查任务", { exact: true }).fill(title);
  await panel
    .getByRole("button", { name: "建立关联任务", exact: true })
    .click();
  await expect(
    panel.getByText("已建立，请到“动作与复查”记录执行、生效时间和观察指标。", {
      exact: true,
    }),
  ).toBeVisible();
  await page.goto("/actions");
  await expect(
    page.getByRole("heading", { name: "动作与复查", exact: true }),
  ).toBeVisible();
  const actionTable = page.locator(".ant-table-wrapper").first();
  let taskRow = actionTable.getByRole("row").filter({ hasText: title });
  for (let pageIndex = 0; pageIndex < 10 && (await taskRow.count()) === 0; pageIndex += 1) {
    const next = actionTable
      .locator(".ant-table-pagination")
      .getByRole("listitem", { name: "下一页" })
      .getByRole("button");
    if (await next.isDisabled()) break;
    await next.click();
    await page.waitForTimeout(150);
    taskRow = actionTable.getByRole("row").filter({ hasText: title });
  }
  await expect(taskRow).toHaveCount(1);
  await taskRow.getByRole("button", { name: "记录执行", exact: true }).click();
  const modal = page.getByRole("dialog");
  await modal.locator("textarea").fill("已核对包装照片及记录，样本核验完成");
  const local = (ms: number) => {
    const d = new Date(ms);
    return new Date(d.getTime() - d.getTimezoneOffset() * 60000)
      .toISOString()
      .slice(0, 16);
  };
  await modal
    .getByLabel("实际生效时间", { exact: true })
    .fill(local(Date.now() - 3600000));
  await modal
    .getByLabel("复查时间", { exact: true })
    .fill(local(Date.now() + 8 * 86400000));
  await modal.getByLabel("观察指标", { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText("订购件数", { exact: true })
    .click();
  await modal
    .getByRole("button", { name: "保存执行记录", exact: true })
    .click();
  await expect(modal).not.toBeVisible();
  await page.goto("/events");
  await row.getByRole("button", { name: "查看证据", exact: true }).click();
  await select("处理操作", "转入观察");
  await select("已完成动作", title);
  await panel
    .getByLabel("处理依据", { exact: true })
    .fill("已记录实际执行，进入观察");
  await panel.getByRole("button", { name: "提交处理", exact: true }).click();
  await expect(
    panel.getByRole("heading", { name: "处理事件 · 待观察", exact: true }),
  ).toBeVisible();
  await select("处理操作", "关闭事件");
  await select("关闭结论", "已知变化");
  await panel.getByLabel("核查依据", { exact: true }).click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText(/^指标快照 /)
    .click();
  await panel
    .getByLabel("处理依据", { exact: true })
    .fill("确认是已登记的合成验收变化，保留快照依据");
  await panel.getByRole("button", { name: "提交处理", exact: true }).click();
  await expect(
    panel.getByRole("heading", { name: "处理事件 · 已关闭", exact: true }),
  ).toBeVisible();
});
