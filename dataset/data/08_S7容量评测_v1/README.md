# S7 容量评测包 v1

该包含两个隔离测试店铺、每店 500 个合成 ASIN、180 个站点日、180,000 条销售日事实、320,400 条订单行、1,000 条 4,096 字符客服记录，以及 1,000 条同日销售修订，共 502,400 行事实。前 20 个 ASIN 每日 40 单形成偏斜负载。`manifest.json` 的 `actual_counts` 是生成器从实际输出计算的数量。

所有行和 sidecar 标记为 `s7-capacity-v1.1`，均为合成数据。来源 ID、ASIN、订单号和客服文本不得用于真实商家账户。`dataset/prepare_capacity_acceptance.py` 会在当前本地后端数据库中创建持久化测试工作区、两家隔离店铺、商品映射，并通过导入 API 上传、解析和发布全部事实。导入期间来源先保持非主状态，避免每个分片发布时自动排队整店扫描；18 个批次完成后再一次性设为主来源。测试工作区的每日扫描计划保持暂停，由下面的容量 runner 单独创建扫描负载。脚本只接受 loopback API 地址。

```bash
cd backend
uv run --env-file ../.env.local python ../dataset/prepare_capacity_acceptance.py --api-base-url http://127.0.0.1:8280
```

脚本将生成两个店铺 UUID 和导入结果，记录在 `dataset/data/08_S7容量评测_v1/validation/capacity_persistent_import_latest.json`。复跑会复用同一隔离工作区和已发布批次。

## 十并发 API 测量

`dataset/run_capacity_acceptance.py` 默认在两家本地隔离店铺中扫描全部 1,000 个 ASIN，最多使用 10 个并发 worker，并对每个扫描发起快照查询。扫描账号文件可采用单账号旧格式，也可采用 `{"accounts":[{"username":"…","password":"…"}]}`；10个线程只有在提供10个不同、具有相应店铺范围授权的真实测试账号时才计为10用户。脚本拒绝把合成数据或凭据发送到非本地地址。`--targets-per-shop N` 仅用于小规模烟雾运行。

```bash
cd backend
uv run python ../dataset/run_capacity_acceptance.py \
  --api-base-url http://127.0.0.1:8280 --credential-file .local/s7_capacity_acceptance_credentials.json \
  --shop-a <隔离店铺A UUID> --shop-b <隔离店铺B UUID>
```

新版 runner 将请求成功、Job 完成、结果正确、采样充分、本地性能、资源观测和目标机验收分开记录；旧 `status` 仅表示扫描请求生命周期已完成，不代表正确性或性能通过。每个任务记录入队到结束、队列等待和 worker 运行；事件、动作、调查、报告、快照、来源、证据检索及详情 API 单独统计 P50/P95/最大值、样本数与非空样本数。数据为空或授权账号少于10个时，采样/性能标为不充分。主机核数/内存会记录，SSD 类型不推断；资源采样按 API、worker、beat、数据库、Redis 分项记录；索引构建时间只从实际索引入口独立计时，不能从扫描耗时或导入总耗时推算。

## 2026-10-05 当前演示包全量复测

唯一一次优化后全量复测报告为 `validation/capacity_run_20261005_current_demo_1000target_10accounts_optimized.json`，使用 `current-demo_2026-10-05_full`（`as_of=2026-10-05`），两个隔离店铺、1,000 ASIN、502,400 行经营事实。10 个授权合成账号发起 1,000 个扫描：请求、Job 和结果正确性均为 **1,000/1,000**，错误 0。全量扫描并发墙钟 **143,673.118ms（143.67秒）**，含业务 API 采样总墙钟 **149,331.830ms**；在本地 900 秒门槛内。缓存队列采样 backlog 峰值 10；Job 入队到终态 P95 1,621.676ms。快照列表查询 P95 223.812ms；15 类业务 API 的 P95 最高为事件列表 **664.041ms**。本次产生 990 个新快照，另 10 个为此前烟测已存在的有效缓存；1,000 个快照均为 `v2-2026-09-23.2/c0`、站点日期 `2026-10-05`。

资源观测采样覆盖 API、worker、beat、PostgreSQL 与 Redis。各组件峰值（CPU%、内存MB、采样数）为：API **125.25 / 340.64 / 29**，worker **75.54 / 286.90 / 29**，beat **2.70 / 96.24 / 29**，PostgreSQL **72.20 / 659.30 / 29**，Redis **1.04 / 10.31 / 116**；CPU 使用率按容器统计口径可超过100%。磁盘类型未测，索引构建不由容量 runner 推算。索引入口单独测得 **1,000 条客服记录 5,459.727ms**（`jieba-0.42.1-feedback-v1`），容量包没有评论记录，因此不是目标评论语料索引验收。可比性热点在隔离偏斜 profile 中由累计 **526ms 降至45ms**，容量扫描只进行一次优化后全量复测。宿主为8 CPU/24GiB，Docker 可用约3 CPU/7.75GiB、容器配置资源限额未设置；不等于目标8核/16GB/SSD环境。容量报告中的 `target_environment` 因而仍为 `pending_external_8_core_16_gb_ssd_environment`。本地结果不覆盖历史失败报告，也不能用于生产或目标环境签收。

历史全量扫描（2026-10-02，报告保留在 `validation/capacity_run_latest.json`）：1,000/1,000 目标、请求错误0、单目标最慢 11,253.677ms、快照列表查询 P95 418.429ms、全量墙钟 994,360.784ms（16分34秒），超过 900 秒门槛 94.361 秒。该报告 CPU、内存、索引构建字段为空，查询 P95 仅代表快照列表。优化后本地演示包测量不能追溯改写这一历史结果。

单次度量前可在只读来源 `insight_acceptance` 上运行一次性克隆 profile；脚本只写 `.local/profile-drills`，不改源库，报告不存 SQL 或原始参数：

```bash
uv run --project backend --env-file .env.local python scripts/profile_metric_scan.py
```

`profile_metric_scan.py` 的合成浏览器库只有少量产品/事实，和 502,400 行容量包不可比。它用于定位计算/查询热点，不替代容量扫描。成熟队列覆盖判断现已在同一计算内缓存重复日期区间；2000 个共享发货日订单的回归测试确认底层日期范围检查不超过8次，原边界/统计结果测试继续通过。当前目标机验收仍需在8核/16GB/SSD环境使用相同数据版本重跑；生产环境不得引用本地容量结果代替。

```bash
python dataset/05_generate_extended_acceptance_packs.py --pack capacity --as-of 2026-10-01 --seed 20261001 --generated-at 2026-10-01T00:00:00Z
python dataset/validate_acceptance_packs.py --as-of 2026-10-01
```
