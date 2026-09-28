from pathlib import Path
import ast, re, hashlib
root=Path.cwd()
p=root/'docs/S1-S7需求代码测试核对表.md'
s=p.read_text()
s=s.replace('本表为静态代码/测试核对与已有执行证据的汇总，本次文档核对未重新跑测试。最近实际结果为新架构后端 54/54、浏览器 9/9 通过，具体名称见附录。', '本表已在 2026-09-23 补入 S1 实施与实际复测结果：新架构后端全量 184/184 通过（51.43 秒），随后语义权限收口专项 6/6 通过；独立 Celery/Redis 故障演练 1/1 通过（14.25 秒）；原有浏览器全套 9/9 通过，新增导出浏览器结果另记。具体证据见第 8 节及《S1补缺执行记录.md》。')
s=s.replace('| S1 基础与权限 | 主干已有，账号维护和全路径权限/并发未闭环 | 部分通过 | 待业务验收 |','| S1 基础与权限 | 账号维护、共享限流、当前产物/导出权限、并发和基础故障已补；后续业务写入/图恢复的横向门槛仍开放 | 基础与当前路径通过，未认定全阶段闭环 | 待业务验收 |')
updates={
'S1-01':('已实现','F01,F02,BND01—03,CON06','测试通过：自动枚举当前 API 禁止匿名、CSRF、过期/停用/退出、共享限流及并发计数','当前入口专项已补；后续新增路由自动纳入匿名矩阵，生产 TLS/Cookie 组合在 S7 仍须实测'),
'S1-02':('部分实现','F03,A03,M08,P04,O02,SCOPE01—03,EXP01—04,S05,S06','测试通过：10 类产物列表/详情/过滤/撤权、部分 SKU 隔离、导出撤权、模型适配器调用中撤权；修复任务结果绕过商品权限','当前实现路径已补；尚未实现的 Agent 写工具及真实供应商调用仍须完成安全联调，不把适配器测试当成真实外发验收'),
'S1-03':('已实现','F04,F05,M16,P03,CON01','测试通过：独立数据库连接并发创建/改映射仅一请求成功，旧版本冲突、历史与父体旧快照保留','当前商品/映射并发已补；完整报告版本链与 Agent 快照冻结尚缺，相关下游验证仍随 S4/S6 保持开放'),
'S1-04':('已实现','A01—03,SEC01—05,CON02,CON08','测试通过：账号创建、重置密码、撤销会话、授权撤销、并发保留最后管理员及浏览器完整操作','本行已定义功能自动验证通过，待业务验收；全账号操作要求覆盖目标账号全部所属工作区的管理员权限'),
'S1-05':('部分实现','F06,M07,CON03—05,CON07,BRK01','测试通过：并发提交/执行、杀真实工作进程后重试、真实 Redis 消息丢失/停机恢复/重复投递；新增 Job 代数及旧回执拒写','基础任务已补；Job 回执代数不等同全部领域写命令与 LangGraph checkpoint 的 fencing，后者仍为 S6-04/S7-03 缺口'),
'S1-06':('部分实现','F04,F05,A03,SEC04,BND04,EXP03','测试通过：CSRF/校验/意外异常统一返回请求编号，秘密不写错误响应/错误日志，审计失败事务回滚；OpenAPI 与构建通过','S1 及导出命令已覆盖；后续预授权执行、事件完整状态变更及 Agent 命令的审计回执未实现，仍须逐命令补齐'),
'S1-07':('已实现','SEC01—05,CON02','测试通过：旧密码失效、多设备会话撤销、新密码登录、普通成员/跨工作区拒绝、弱密码/冲突、审计失败回滚及管理员自身退出','自动验证已补，待业务验收；撤销会话不等于撤销业务授权或取消已授权后台任务'),
'S4-08':('已实现','EXP01—04','测试通过：HTML/JSON 报告、按当前筛选导出 CSV，当前权限、版本/引用/限制、HTML 转义、CSV 公式注入防护、失败不输出文件','导出链路已补；导出的报告质量仍依赖 S4 其他未完项，不能视为标准报告整体完成'),
'S7-03':('部分实现','F06,G04,CON03—05,CON07,BRK01','基础任务真实多连接/杀进程、独立 Celery/Redis 消息丢失/停机恢复/重复消息演练通过','仍缺数据库实际断线、跨所有业务副作用的写后崩溃、LangGraph 旧持锁进程与多维预算联合恢复；不认定综合故障验收通过'),
}
lines=s.splitlines()
for i,line in enumerate(lines):
 if not line.startswith('| S'):continue
 parts=line.split('|'); m=re.match(r'\s*(S\d-\d\d)',parts[1])
 if m and m.group(1) in updates:
  state,tests,result,gap=updates[m.group(1)]
  parts[2]=' '+state+' ';parts[4]=' '+tests+' ';parts[5]=' '+result+' ';parts[6]=' '+gap+' '
  lines[i]='|'.join(parts)
s='\n'.join(lines)+'\n'
s=s.replace('这些浏览器测试没有覆盖：完整报告支持/反证、Action闭环、Agent自主决策、澄清/接续、报告导出、生产恢复。','原有九条浏览器测试没有覆盖：完整报告支持/反证、Action闭环、Agent自主决策、澄清/接续、生产恢复。新增报告导出验证单列于第 8 节。')
s=s.replace('当前没有 Git 提交号。以下摘要用于辨认本次静态核对的关键文件；后续变更需要更新对应行，不能把旧测试结果自动套到新代码。','当前没有 Git 提交号。以下摘要已随本轮 S1 修改刷新；只有明确列出的复测范围可引用测试结论。')
for m in list(re.finditer(r'\| \[(backend/[^\]]+|compose\.v2\.yml)\]\([^\n]+?\) \| `([0-9a-f]{64})` \|',s)):
 f=root/m.group(1)
 if f.exists():s=s.replace(m.group(2),hashlib.sha256(f.read_bytes()).hexdigest())
section='''
## 8 2026-09-23 S1 补缺实测增量

本轮修复与剩余项详见 [S1补缺执行记录](</Users/Admin/Desktop/商品评论智能诊断 agent/docs/S1补缺执行记录.md>)。测试计数包含参数化场景，不代表同等数量的需求条款。以下编号只增加，不改动原 F/A/I/M/P/R/S/O/G 编号。

| 编号 | 新增测试入口 |
|---|---|
'''
prefixes={'SEC':'backend/tests_v2/test_account_security.py','BND':'backend/tests_v2/test_s1_boundaries.py','CON':'backend/tests_v2/test_s1_concurrency.py','SCOPE':'backend/tests_v2/test_s1_scope_matrix.py','EXP':'backend/tests_v2/test_exports.py','BRK':'backend/tests_integration/test_s1_broker_recovery.py'}
for prefix,file in prefixes.items():
 for index,n in enumerate([n for n in ast.parse((root/file).read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')],1):
  section+=f'| {prefix}{index:02} | [{n.name}](<{root/file}:{n.lineno}>) |\n'
for index,n in enumerate([n for n in ast.parse((root/'backend/tests_v2/test_semantic.py').read_text()).body if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')],1):
 if index>=5:section+=f'| S{index:02} | [{n.name}](<{root}/backend/tests_v2/test_semantic.py:{n.lineno}>) |\n'
section+='''
- BRK01 使用独立临时 Redis 容器、真实 Celery worker 和独立测试数据库；没有停止日常开发队列。模拟重投时仅提前 Outbox.sent_at 以跳过等待，实际停机、消息丢失和消费均真实执行。演练结束删除临时容器。
- CON04 实际杀死执行 run_job 的 Python 子进程；这与 BRK01 的真实 Celery 消费是两种互补验证，不宣称已测试 Celery 子进程全部退出方式。
- CON07 是模拟代数被接管后的拒写测试，不宣称已执行数据库断线故障。
- 浏览器账号维护场景已扩充为创建成员/授权→成员登录→强制退出→旧密码仍可重新登录→重置密码→旧密码拒绝→新密码登录。
- 新增报告与事件下载浏览器场景：[exports.spec.ts](</Users/Admin/Desktop/商品评论智能诊断 agent/frontend/e2e/exports.spec.ts>)，最终执行结果见本轮记录。
- 首次授权浏览器失败暴露长列表不可定位，已加授权店铺搜索；首次导出浏览器失败先修正测试按钮名称，继而发现两个表单生成重复字段 ID，已为语义运行/原文检索设置独立表单名。失败原因保留，不把重跑直接写成首次通过。
'''
s=s.split('\n## 8 2026-09-23 S1 补缺实测增量')[0]+section
p.write_text(s)
