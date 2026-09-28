# 航标 Insight

> **当前开发基线（2026-09-21）**：按 V2 与总体技术方案迁移，阶段目标及验收方式见 [开发阶段与验收计划](docs/开发阶段与验收计划.md)。主体架构迁移已由用户确认；S1 基础功能、S2 主导入链路已实现并验证，S3 开发中。新工作台地址 `http://127.0.0.1:5184`；操作与验证范围见 [阶段验收记录](docs/阶段验收记录.md)。下方 Stage 5/6 状态属于历史原型，不代表本次 FR-01—13 已完成或已验收。

```yaml
stage6a-p0-engineering: accepted
stage6a-p0-semantic-release: pending
stage6b-p1-claim-store: accepted
stage6b-p1-review-signal: accepted
stage6b-p1-retrieval-rag: qdrant_experimental
stage6b-core-mvp: accepted_local
stage6b-real-e2e: deferred_external_model
keyword-retrieval: default
char-bigram-hybrid: experimental_fallback
qdrant: smoke_tested
stage6b-business-enrichment: deferred_external_model
stage6b-local-mode: deterministic_local
stage6b-core-productization: completed
stage6b-local-release: rc1
```

面向跨境电商运营的商品经营异常诊断工作台。当前版本完成 Stage 5 可运行纵向切片，并落地 Stage 5.2 Metric Engine、Stage 5.3 Anomaly Engine、Stage 5.4 Multi-Metric Engine 与 Stage 5.5A Review Semantic Layer 的模型选型：从经营总览发现异常，使用确定性算法核对单指标和联合经营状态，再进入调查轨迹回查评论原文证据。

## 文档

- **需求与设计主文档**：[需求分析与开发范围冻结基线 V2](docs/阶段1_需求分析与MVP设计_V2.md)。2026-09-20 修订为一次性交付范围，统一经营核查、数据口径、事件队列、证据报告、处理复查与验收，不再按产品版本拆分核心能力。**本文描述的是实现现状，V2 描述的是目标态；两者冲突时以 V2 为准。**
- **总体技术设计与实施方案**：[总体技术设计与开发实施方案](docs/总体技术设计与开发实施方案.md)。从需求独立设计数据接入、计算、追溯及运营处理；第 11 节补齐完整技术推荐；11.3 节拆解 46 个业务任务，11.12 节明确 LangGraph 自主调查、模糊指令定位、受控内部写入与恢复契约。自主调查范围、分级执行权限及推荐主体架构已确认；方案中的早期“待确认”状态以 2026-09-21 开发计划确认记录为准，具体依赖兼容、模型与故障验证尚未全部完成，不受历史代码及模型选择约束。
- **运行时现状与本地发布**：[运行时状态与本地发布](docs/运行时状态与本地发布.md)。
- **数据源字段**：[数据源导入说明](docs/数据源导入说明.md)。
- **语义模型选型**：[语义模型选型记录](docs/语义模型选型记录.md)。
- **前端交付说明**：[前端开发交付说明](docs/前端开发交付说明.md)。描述历史单页交付范围，不作为新开发验收依据；目标页面以 V2 第 12 节为准。

## 当前能力

- 经营总览：订单量、退款率、评分、评论量及月度趋势。
- 商品中心：商品健康度、异常数量与数据覆盖。
- 异常中心：按风险等级筛选并进入诊断。
- AI 深度诊断：展示路由、指标核对、假设、检索、评估与停止原因。
- 评论洞察：标准化业务 Review 关键词检索和评论原文回查；Topic × Polarity 聚合接口已接入 Prediction Claim Store（未把预测当作 Ground Truth）。
- 产品化操作：评论正/负向筛选、异常列表 CSV 导出、调查 JSON 导出与本地继续追问入口。
- 本地数据源：支持经营指标、退款原因、售后和库存 CSV 导入，以及只读探测本地 SQLite；字段和质量校验见 [数据源导入说明](docs/数据源导入说明.md)。
- 数据边界：GMV、CVR、广告 ROAS、库存可售天数在缺少事实数据时显示为待接入。
- Metric Engine：支持日、周、月查询与聚合，以及当前区间和 Baseline 对比。
- Anomaly Engine：支持 Baseline Delta、Rolling Z-Score、Robust Z-Score/MAD 和 Change Point。
- Multi-Metric Engine：联合订单与退款信号，识别健康增长、增长伴随售后风险、综合恶化、需求走弱、经营恢复、经营反转、混合信号和稳定状态。

经营指标来自可复现的 synthetic 数据；评论及标签来自项目内 Amazon Reviews 2023 数据集。Stage 5 调查页是确定性演示轨迹，不读取黄金诊断预期，也不代表真实 LangGraph Agent 已接入。

Stage 5.5A 当前冻结 `DeepSeek v4 flash + semantic-prompt-v1.0 + taxonomy-v1.1` 作为主要语义候选，Qwen 3.5 9B 作为本地开发和降级模型。语义抽取定位为离线任务，运行时 API 不读取 Golden Review 标签，也不会在每次 Agent 调查中实时批量调用模型。完整决策见 [语义模型选型记录](docs/语义模型选型记录.md)。

DeepSeek 凭据仅从 `DEEPSEEK_API_KEY` 环境变量读取。可复制 `backend/.env.example` 作为本机配置参考，但不要把真实 Key 写入仓库。

## 本地启动

需要 Node.js 20+、pnpm 与 Python 3.11+，并安装 `uv`。

```bash
make install
make dev
```

启动后访问 <http://127.0.0.1:5174>。API 文档位于 <http://127.0.0.1:8200/docs>。本机 5173 已被其他 Globex 工作区占用，API 端口 8000 留给 PlanPilot。

也可以分别启动：

```bash
cd backend
uv sync --dev
uv run uvicorn app.main:app --host 127.0.0.1 --port 8200
```

```bash
cd frontend
pnpm install
pnpm dev
```

## 验证

```bash
make test
make build
make preflight
```

本地发布、SQLite 备份/恢复和 Docker 启动方式见 [运行时状态与本地发布](docs/运行时状态与本地发布.md)。
本地验收记录见 `backend/artifacts/semantic_eval/stage6b_local_release_report.json`。

## 后续阶段

Stage 6A P0 技术闭环已完成，保留两个语义发布债务（A 最终答案措辞、B abstain 语义）并行推进 P1 Claim Store。广告、价格、库存、流量和促销等数据需要在获得真实数据源后接入，当前界面不会为这些指标生成占位数值。

V2 冻结范围包含销售与流量、转化指标、退款退货、可获得的售后反馈、库存／可售状态、购物车、价格促销和评论证据；数据缺失时的能力边界见第 8 节。广告归因、竞品分析和自动外部处置明确不在范围内。全部 FR 统一验收，W0 至 W6 仅为工程工作包。

Stage 6A 已接入 Evidence Guard、Investigation State、Task Router、Tool Registry、真实 Planner/Tool Loop、SQLite 持久化与 SSE。Stage 6B 已完成 Claim Store、ReviewSignal、Keyword 默认检索、Qdrant smoke 和 Review Detail 回查；Qdrant 仍为实验后端，真实业务 Enrichment Pilot 与三类黑盒 E2E 因 DeepSeek 凭据欠费而阻塞。运行时不读取 Golden，也不把 Prediction 当作 Ground Truth。
