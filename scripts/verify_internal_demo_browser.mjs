// Actual browser checks against the owned historical demo; no frontend edits.
import { createRequire } from 'node:module';
import fs from 'node:fs';
import path from 'node:path';

const root = process.cwd();
const require = createRequire(path.join(root, 'frontend/package.json'));
const { chromium } = require('@playwright/test');
const run = path.resolve(process.argv[2]);
const output = path.resolve(process.argv[3]);
const ownedRoot = path.join(root, '.local/acceptance-artifacts') + path.sep;
if (!run.startsWith(ownedRoot) || !output.startsWith(ownedRoot) || fs.existsSync(output)) {
  throw new Error('owned running demo and new output required');
}
const state = JSON.parse(fs.readFileSync(path.join(run, 'state.json')));
const account = JSON.parse(fs.readFileSync(state.credentials_file)).find(row => row.username === 'accept_admin');
fs.mkdirSync(output, { recursive: true });
const browser = await chromium.launch({ headless: true });
const results = [];
const errors = [];
try {
  const page = await browser.newPage();
  page.on('pageerror', error => errors.push(error.message));
  await page.goto(state.frontend_url + '/reports');
  await page.getByLabel('用户名', { exact: true }).fill(account.username);
  await page.getByLabel('密码', { exact: true }).fill(account.password);
  await page.getByRole('button', { name: '登录', exact: true }).click();
  await page.getByRole('heading', { name: '调查报告', exact: true }).waitFor();
  await page.getByRole('combobox', { name: '选择店铺' }).click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: '发布验收 04 全量经营包' }).click();
  await page.getByLabel('查询商品', { exact: true }).click();
  await page.locator('.ant-select-dropdown:visible .ant-select-item-option').filter({ hasText: 'B07P9S6R9Y' }).first().click();
  await page.getByLabel('开始日期', { exact: true }).fill('2023-06-04');
  await page.getByLabel('结束日期', { exact: true }).fill('2023-09-02');
  await page.getByLabel('问题词或原文短语').fill('slow charging');
  const responsePromise = page.waitForResponse(response => response.url().endsWith('/api/v1/evidence-search/') && response.request().method() === 'POST');
  await page.getByRole('button', { name: '查询证据', exact: true }).click();
  const response = await responsePromise;
  const evidence = await response.json();
  if (response.status() !== 200 || evidence.scope.asin !== 'B07P9S6R9Y' || evidence.scope.start !== '2023-06-04' || evidence.scope.end !== '2023-09-02') {
    throw new Error('actual browser request scope or API response mismatch');
  }
  results.push({ page: 'reports', real_api_status: response.status(), request_scope: evidence.scope,
                 hit_count: evidence.items.length, original_reference_count: evidence.items.filter(item => item.raw_row_id && item.fact_id).length });
  await page.goto(state.frontend_url + '/investigations');
  await page.getByRole('heading', { name: '自主调查', exact: true }).waitFor();
  await page.getByLabel('问题', { exact: true }).fill('请调查 B07P9S6R9Y 的充电慢反馈，日期范围 2023-06-04 至 2023-09-02。区分现象与原因，并引用原文。');
  const investigationResponse = page.waitForResponse(response => response.url().endsWith('/api/v1/investigations/') && response.request().method() === 'POST');
  const completedInvestigation = page.waitForResponse(async response => {
    if (!/\/api\/v1\/investigations\/[^/]+\/$/.test(response.url()) || response.status() !== 200) return false;
    const body = await response.json();
    return body.status === 'completed';
  }, { timeout: 60000 });
  await page.getByRole('button', { name: '开始调查', exact: true }).click();
  const queued = await investigationResponse;
  if (queued.status() !== 202) throw new Error('actual browser investigation was not queued');
  const completed = await completedInvestigation;
  const completedBody = await completed.json();
  await page.getByText('实际调查规划模式：规则调查', { exact: false }).waitFor({ timeout: 60000 });
  const technicalDetails = page.getByText('执行预算与工具权限（技术详情）', { exact: true });
  await technicalDetails.waitFor({ timeout: 60000 });
  await technicalDetails.click();
  await page.getByText(/调查预算：剩余/).waitFor({ timeout: 60000 });
  await page.getByText('调查工具权限矩阵', { exact: true }).waitFor();
  results.push({ page: 'investigation-execution', api_status: queued.status(), job_id: (await queued.json()).id,
                 budget_and_tool_receipts_visible: true, planner: 'deterministic', completed_status: completedBody.status });
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    await page.screenshot({ path: path.join(output, `investigation-result-${width}.png`), fullPage: true });
  }
  for (const width of [390, 768, 1440]) {
    await page.setViewportSize({ width, height: 1000 });
    for (const route of ['reports', 'investigations', 'sources', 'products', 'jobs']) {
      await page.goto(state.frontend_url + '/' + route);
      const headings = { reports: '调查报告', investigations: '自主调查', sources: '数据源', products: '商品映射', jobs: '后台任务' };
      await page.getByRole('heading', { name: headings[route], exact: true }).waitFor();
      await page.waitForLoadState('networkidle');
      await page.waitForFunction(() => document.querySelectorAll('.ant-spin-spinning').length === 0);
      await page.screenshot({ path: path.join(output, `${route}-${width}.png`), fullPage: true });
      const dimensions = await page.evaluate(() => ({ viewport: innerWidth, scroll: document.documentElement.scrollWidth }));
      if (dimensions.scroll > width) throw new Error(`document overflow on ${route} at ${width}`);
      results.push({ page: route, width, dimensions });
    }
  }
  if (errors.length) throw new Error('browser JavaScript errors: ' + errors.join('; '));
  fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ status: 'passed', scope: 'actual historical review query and navigation at three widths; no independent semantic correctness claim', results, errors }, null, 2));
  console.log(JSON.stringify({ status: 'passed', screenshots: 18, real_review_query: true, real_investigation_execution: true }));
} catch (error) {
  fs.writeFileSync(path.join(output, 'result.json'), JSON.stringify({ status: 'failed', error: error.message, results, errors }, null, 2));
  throw error;
} finally {
  await browser.close();
}
