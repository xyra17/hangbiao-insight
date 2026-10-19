# 航标前端残余排查与界面优化交付

日期：2026-10-12。入口：`frontend/src/main.tsx → workbench/Workbench.tsx`。

## 已完成

| 复现问题 | 修复 | 验证 |
|---|---|---|
| 报告/任务列表未请求 page，50条后不可达 | 服务端分页，每次50条，总数/页码/局部滚动；店铺改变即时回第一页 | 51条 fixture、跨页、切店铺，浏览器与单元测试 |
| 动作任务、变更提案、观察线索也只有第一页 | 同一分页工具接入三类列表，创建任务后刷新首屏，不拉取全量历史 | 真实页面第51条可达，请求次数受控 |
| 列表刷新后末页消失 | 成功空末页按count回有效页；DRF404回第一页；等待本次读取完成再判断，避免旧404缓存阻止恢复页 | 成功空页/404/恢复51条的回归测试，浏览器重读 |
| completed任务仅含目标ID仍显示等待 | 按真实状态显示“执行已结束；暂无摘要”，失败/取消/retry_wait/未知分别中文显示 | ID不作为质量通过证明，状态测试 |
| 报告列表GET失败被空状态掩盖；生成报告GET失败无重试动作 | 明确错误区与GET重读按钮；列表可主动只读刷新 | 列表/详情500 fixture、无新增POST断言 |
| EvidenceSearch未知或缺失match_kind标为相关命中 | 只有relevant_hit显示相关命中；scope_context显示上下文；未知显示“相关性待确认” | 四类单元测试与当前证据截图 |
| 主按钮加载图标影响可访问名称 | 固定明确的aria-label，aria-busy与真实提交状态一致 | 连续调查/取证操作，键盘激活 |
| 窄屏报告表格挤成细列；固定宽表单层级不清 | 表格局部横向滚动；表单网格响应式；主操作与辅助说明分层 | 390/768/1440截屏、文档无水平溢出、局部表格可滚动 |

相邻列表核查：Source的导入批次/行及Event事件主列表已有服务端分页，没有此次“只读前50条”的同类缺口；Source目录/商品选项的既有完整目录加载未扩大修改。动作与观察历史的缺口已在受控浏览器复现并修复。

## 界面落地

使用已安装 `refero-design`，无需新增skill或MCP。无Refero在线工具，本次研究依据为该技能的 bundled typography/color/craft-details/anti-ai-slop/visual-workflow，以及最新before实拍和既有航标CSS。研究、参考锁与具体决策见 `DESIGN.md`。

- 保留深海军蓝导航、白色数据面板、青绿色主要操作；统一CSS和Ant Design主色，hover区分。
- 统一系统无衬线页标题/正文/表格字阶、控件高度与数字对齐，减少与主体竞争的长介绍。
- 调查表单按模式/对象/执行步数分组；内部草稿授权与主操作独立区域。费用/区域/传输授权提示和外部操作限制持续可见。
- 报告先展示取证任务与历史版本，原文检索在独立区域；SKU/语言/主题/极性进入可展开筛选。
- 保持失败显著、未知准确、部分完成不伪装成成功；报告“已发布”用中性标记，不暗示质量已通过。
- 浅色信息提示保留足够文字对比；复合选择器、按钮和输入保留键盘焦点；移动表单改为单列，数据表仅内部滚动。

## 修改文件

本轮前端代码：

- `frontend/src/workbench/useServerPage.ts`、`ListError.tsx`、`JobPage.tsx`（新增）
- `ReportsPage.tsx`、`InvestigationPage.tsx`、`EvidenceSearch.tsx`、`Workbench.tsx`、`ActionsPage.tsx`、`EventPage.tsx`、`executionStatus.ts`、`workbench.css`
- `useServerPage.test.tsx`、`EvidenceSearch.test.tsx`（新增）；`QualityWorkflow.test.tsx`、`executionStatus.test.ts`

交付与验证独立保存在 `frontend/verification/frontend-polish-20261012/`。上一轮 `frontend-quality-20261012` 报告、输出和manifest未覆盖。共享目录原有改动保留，未reset、未另建丢失当前实现的worktree、未批量提交。

## 验证结果

在frontend目录：

```sh
npm test -- --reporter=default
npm run build
./node_modules/.bin/playwright test --config verification/frontend-polish-20261012/playwright.controlled.ts
```

- 6个测试文件、46项测试通过；原有30项全部保留通过，新增16项。
- TypeScript与Vite构建通过。
- 11项浏览器测试通过：3个宽度的完整代表性状态截图 + 8项工作流/键盘/分页行为验收。
- 原始输出：`test-output.txt`、`build-output.txt`、`browser-output.txt`；浏览器结构化结果：`browser-results.json`。
- 请求证据：`investigation-requests.json`、`report-requests.json`、`pagination-requests.json`、`neighbor-requests.json`，以及各宽度 `after/*-browser-log.json`。最终测试均断言pageerror为空；console保留了主动注入500/404导致的浏览器资源错误，未将这些预期错误冒充零console错误。
- 调查默认请求deterministic；只有显式选择才发送DeepSeek。终态停止的是任务状态GET轮询；已有数据修订历史组件仍按既有机制只读检查刷新记录。
- 网络重读只发送GET；连续提交、切店铺不沿用旧结果；完成/失败后列表刷新回归保持通过。
- 浏览器场景完全拦截 `/api/v1/**`，未配置接口返回受控501；外部请求阻断。自有Vite端口5197没有后端代理，测试完成后自动停止。不连接真实供应商或共享数据库，未读取`.env.approved`，未生产发布。
- `git diff --check -- frontend`通过。manifest保存当前改动源码和本轮证据SHA-256，证据对应当前源文件；旧交付manifest保持历史时点含义。

## 最新截图与预览

`before/` 与 `after/` 各24张PNG，均为本轮Chromium真实截图，8种状态 × 3种宽度：登录、调查填写、调查结果、报告列表、报告结果、原文证据、列表错误、后台任务。旧acceptance截图未当作当前证明。

打开 `index.html` 可按页面和宽度对照，图片区域独立滚动，点击可查看原始尺寸。本地只读截图预览地址：`http://127.0.0.1:5198/index.html`。它展示截图，不发送业务或模型请求。服务仅绑定本机地址。

重启预览（如已停止）：

```sh
python3 -m http.server 5198 --bind 127.0.0.1 --directory 'frontend/verification/frontend-polish-20261012'
```

### 视觉QA

对照 `DESIGN.md` 参考锁，已人工检查当前桌面调查填写/结果、移动登录/调查结果/报告错误和截图预览中的列表/证据。海军蓝导航、白面板、青绿色操作角色保持；授权、未知扣款、未知结论、部分核查均保留。390/768/1440文档宽度断言无页面水平溢出；键盘选择器焦点和提交激活通过；语义小标签的浏览器 computed color 对比度断言通过，部分完成6.26:1、相关命中6.68:1（见contrast-*.json）；小屏表格可在局部横向滚动。未发现裁切、文字重叠或关键限制被隐藏。检查范围为代表性fixture，未宣称覆盖每一种真实数据组合。

## 后端契约仍待补齐

1. 按用户/店铺提供调查模式能力清单与稳定拒绝原因；选择模式不能替代create/resume/clarify时的服务端授权、区域与预算校验。
2. 提供稳定 requested/observed planner 执行摘要与核验状态，避免历史或失败运行只能依靠零散决策回执。
3. 费用提供币种、金额类型（预留上限/usage估算/确认扣款）、已知状态及未确认调用数量，聚合按币种拆分。当前仍显示实际扣款未知。
4. 提供run/job统一错误code、安全中文消息、失败阶段与可执行动作；只有现有稳定截断/空输出/不完整代码被解释，预算/配置/区域问题不靠猜字符串。
5. 若需要状态/时间过滤历史，后端应提供明确筛选契约；本轮只使用已支持店铺范围，不在当前页伪装全量过滤。

本轮交付修复前端功能并优化视觉，不代表召回、语义判定或业务质量已过关。完整上线仍 `not_ready`，真实业务试点尚不可实现。
