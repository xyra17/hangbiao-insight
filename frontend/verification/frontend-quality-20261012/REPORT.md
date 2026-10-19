# 智能质量主线前端修复交付（2026-10-12）

## 范围与修改

本次只修改以下前端源文件；保留共享目录原有未提交实现。未改后端、脚本、模型配置、OpenAPI、历史冻结证据，未提交或发布。

- `frontend/src/workbench/InvestigationPage.tsx`：调查模式默认为规则调查，POST 明确携带 `planner_provider: deterministic`；用户主动选择 DeepSeek 时发送 `deepseek`。文案说明后端仍须校验供应商、区域、传输授权和预算，不声明 DeepSeek 已可用。没有自动降级或模型重试。调查规划模式与评论语义检索配置明确区分。
- 调查实际规划模式仅依据后端决策的 `execution_origin` 与供应商响应回执显示，所选模式、planner 名称或任务完成状态都不作为执行证明。记录不足显示未知；混合记录分别显示。
- 调查排队、执行中、正常完成、部分完成、失败、取消、待澄清均显示中文；`status=completed` 且 `result.completion_status=partial/blocked` 时显示核查缺口，接续权限仍依据真实服务端状态。执行结束不表示结论确认，已有证据/检索边界提示保留。
- 识别预算账本 metadata 中稳定的 `output_truncated`、`empty_output_text`、`response_incomplete` 错误代码，并提供人工核查建议。没有结构化原因时保留后端说明，不按异常文本猜测预算、密钥或授权问题。接续、澄清说明沿用原模式与剩余预算、可能涉及费用。
- 当前 API 没有币种及扣款确认，实际模型扣款显示未知；没有将 `model_cost=0`、usage 估算或预留额度当作实际扣款。次数、token、预算单位、工具权限移至折叠技术详情。
- `frontend/src/workbench/ReportsPage.tsx`：任务依据最新状态轮询，queued/running/retry_wait 继续，终态停止；完成和失败自动刷新当前店铺的 semantic-runs 与 reports 列表。只对完成任务的 report_id 读取新报告。
- 开始新任务立即清除旧任务/已选报告；历史版本请求迟到时不得重新展示旧报告。店铺变化重置页面状态，查询键包含店铺。调查页同步处理具体同类状态轮询及旧任务隔离问题。
- 两页状态读取网络异常暂停轮询，关闭自动窗口聚焦/重连刷新；手动按钮只重新读取状态，不重新发起模型任务。
- `frontend/src/workbench/executionStatus.ts`：集中状态文案、执行记录识别及稳定错误代码解释。
- `QualityWorkflow.test.tsx` 与 `executionStatus.test.ts`：新增行为及边界测试。

## 验证

在 frontend 目录运行：

```sh
npm test -- --reporter=default
npm run build
```

- 4 个测试文件、30 项测试通过。其中新增 18 项，现有 12 项全部保留通过。
- `tsc -b` 和 Vite 构建通过。
- 本次修改文件 `git diff --check` 通过。
- 实际 React/Ant Design 组件 + React Query 在 jsdom 中运行，API 请求通过 vi mock 控制；相邻检索/历史组件隔离。覆盖模式请求参数、默认不选择 DeepSeek、后端拒绝无自动回退、所有调查终态和待澄清、费用/模式未知、执行完成但核查部分完成、连续两个成功任务、第二任务拒绝、历史请求迟到、切店铺时未完成请求、queued→running→retry_wait→终态轮询和两个列表刷新、网络失败停止及手动只读重试。
- `test-output.txt` 与 `build-output.txt` 保存原始命令输出，`verification-manifest.json` 保存本次源文件和输出 SHA-256。
- 存在原有 Ant Design `Space.direction` 弃用提示，不影响测试和构建。
- 未启动共享服务、访问数据库、读取 `.env.approved`、调用真实供应商或运行依赖共享服务的浏览器 E2E。此次证据是受控前端行为验证，不是供应商集成/线上验收。

## 仍需后端契约（未并发修改）

以下为建议契约，当前接口不应被视为已提供：

1. 按用户和店铺返回调查模式能力，例如 `investigation_capabilities.planner_modes[]`，包含 `provider`、`selectable`、稳定 `reason_code` 与安全中文说明。可选不等于授权成功，每次 create/resume/clarify 仍须服务端重新校验区域、数据传输授权、供应商及预算。现状只有请求 ChoiceField 与执行时拒绝，前端显示“需后端校验/未确认”，不推测授权。
2. 当前 `decisions.execution_origin` 及 `model_receipt` 可核实已记录的规划决策；建议在调查详情提供稳定的 `execution.requested_planner`、`observed_planners`、`verification_status` 与未观察到执行的原因，避免历史记录或模型失败时被误当已调用。规划模式不得替代检索相关性供应商状态。
3. 费用：提供 `currency`、`amount`、`amount_kind`（reserved_upper_bound / usage_estimate / confirmed_charge）、`known`、结算/供应商回执状态以及未确认调用数量；聚合必须按币种拆分，未知项不当零。目前 `budget.model_cost`、条目 `cost` 和 `charge_outcome=recorded_by_usage` 不足以证明实际扣款，也未提供币种，因此本次只显示实际扣款未知。
4. 错误：在 run/job 的稳定结构化字段提供 `error.code`、安全用户消息、失败阶段、是否仅可重读、是否必须重新授权。账本已有输出截断/空响应/不完整代码可解释；预算不足、配置缺失、区域或传输授权失败等缺少统一公开代码，现阶段不凭字符串伪造原因。
5. 显式 `allowed_actions` 可区分部分核查的完成任务与真正可接续的 partial/cancelled 运行；当前按已有后端状态限制按钮，不扩大可执行动作。

## 交付边界

本次修复前端请求和状态衔接，不声称修复召回、语义判定或业务质量。完整上线状态仍为 `not_ready`，真实业务试点尚不可实现，不阻塞此次前端交付。
