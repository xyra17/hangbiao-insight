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
test("跨问题族人工合并与拆分保留原事件", async ({ page }) => {
  const user = accounts.find(
    (a: { username: string }) => a.username === "accept_admin",
  );
  await page.goto("/events");
  await page.getByLabel("用户名", { exact: true }).fill(user.username);
  await page.getByLabel("密码", { exact: true }).fill(user.password);
  await page.getByRole("button", { name: "登录", exact: true }).click();
  await page.getByLabel("选择店铺", { exact: true }).click();
  await page.getByText("验收 D：事件处理闭环 / US", { exact: true }).click();
  await page
    .getByRole("row")
    .filter({ hasText: fixture.asin })
    .filter({ hasText: "合成流程验收事件" })
    .getByRole("button", { name: "查看证据", exact: true })
    .click();
  const panel = page.locator(".ant-drawer-body");
  await expect(
    panel.getByRole("cell", { name: "合成关联退货事件", exact: true }),
  ).toBeVisible();
  const select = async (label: string, option: string) => {
    await panel.getByLabel(label, { exact: true }).click();
    await page
      .locator(".ant-select-dropdown:visible")
      .getByText(option, { exact: true })
      .click();
  };
  await select("分组操作", "合并为问题组");
  await panel
    .getByLabel("其他事件（当前事件始终选中）", { exact: true })
    .click();
  await page
    .locator(".ant-select-dropdown:visible")
    .getByText(/合成关联退货事件/)
    .click();
  await page.keyboard.press("Escape");
  await panel
    .getByLabel("问题组名称（合并时必填）", { exact: true })
    .fill("浏览器包装问题组");
  await panel
    .getByLabel("合并或拆分依据", { exact: true })
    .fill("核查相同商品不同信号，保留独立证据");
  await panel
    .getByRole("button", { name: "保存问题组调整", exact: true })
    .click();
  await expect(
    panel.getByText("当前问题组：浏览器包装问题组", { exact: true }),
  ).toBeVisible();
  await select("分组操作", "从原问题组拆分");
  await panel
    .getByLabel("合并或拆分依据", { exact: true })
    .fill("核查后两个问题分别跟踪");
  await panel
    .getByRole("button", { name: "保存问题组调整", exact: true })
    .click();
  await expect(
    panel.getByText("同一商品的其他异常", { exact: true }),
  ).toBeVisible();
  await expect(
    panel.getByRole("cell", { name: "合成关联退货事件", exact: true }),
  ).toBeVisible();
});
