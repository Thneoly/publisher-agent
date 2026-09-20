---
name: publish-ops
description: 发布 Agent 的运维命令面：生成/修改发布计划（plan.yaml）、注册计划任务、暂停恢复、查状态、故障处置。当用户要"排期发布/定时发文/查看发布进度/发布出问题了"时使用。
---

# /publish-ops 操作手册

核心原则：**这个 skill 只读写 plan.yaml 与决策文件，永不直接外发**——外发唯一写者是 driver.py tick。

## plan：把用户的排期意图变成 plan.yaml

用户说人话（例：「这 5 篇，明早 9 点开始每天一篇，知乎晚一天跟上」），你生成/修改 `plan.yaml`（schema 见 `plan.example.yaml`）：
- `id`：稳定 slug（文件改名不影响状态）
- `file`：引擎 cwd 相对路径
- `platforms`：`juejin: {}` 投掘金；`zhihu: {after: juejin}` 表示掘金上线 ≥24h 后发知乎
- 驳回重写的新条目加 `flags: {rewrite_of: 旧slug}`，并把旧条目状态说明写进对话
- 生成后必跑 `uv run python -m publisher_agent.driver tick --dry` 给用户看投影结果，确认后再 deploy

## deploy：注册计划任务（必须用户确认）

用 PowerShell（`StartWhenAvailable` 让睡眠/关机错过的触发醒来即补；绕开 schtasks 的 MSYS 路径坑）：

```powershell
$action  = New-ScheduledTaskAction -Execute 'D:\FDE\publisher-agent\tick.bat'
$trigger = New-ScheduledTaskTrigger -Daily -At 09:15
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable
Register-ScheduledTask -TaskName 'BlogAgent_tick_am' -Action $action -Trigger $trigger -Settings $settings
```

时段建议对齐 cadence（掘金 2 篇/日→早晚各一 tick；知乎 1 篇/日→晚 20:00 后，min_gap 25h 防贴 24h 限频边界）。任务名写进 `agent.yaml` 的 `tasks:`（队列发完 driver 会 disable 它们）。

`tick.bat`：`cd /d D:\FDE\publisher-agent && uv run python -m publisher_agent.driver tick >> _state\tick.log 2>&1`

## 常用命令

| 命令 | 作用 |
|---|---|
| `driver tick [--dry]` | 跑默认流水线（= run default；schtasks 用它） |
| `driver run reviewed [--dry]` | 跑指定流水线（pipelines/reviewed.yaml：发布前人工门禁） |
| `driver gate approve [id]` / `reject` | 放行/否决等待中的门禁（唯一等待中时 id 可省） |
| `driver status [--json]` | 队列+门禁+流水线一览（--json 供看板/工作流消费） |
| `driver report` | 台账汇总（含心跳/上次流水线） |
| `driver doctor` | 自检：引擎/Cookie/CDP/文件缺失/流水线清单 |
| `schtasks /Change /TN <名> /DISABLE 或 /ENABLE` | pause / resume（队列不动） |

## 与 publish-workflow 的分流（一句话）

要**判断、起草、串全链路**（生产→预检→干跑→门禁报告→发布→事后）→ 跑本仓库 `workflows/publish-workflow.js`；
只是**查状态、改排期、暂停、救火** → 直接 driver / 本命令面。外发永远只归 driver。

## 流水线编排（pipelines/*.yaml）

- 步骤原语七种：`reconcile / poll / verify-unknown / select / gate / publish / disable-tasks`
- 步骤参数：`when: queue_done|picked`、`on-fail: continue|abort`；gate 额外 `id/platform/timeout_h`
- 无 pipelines/ 时内置 default 与旧 tick 行为逐字等价；改流水线=改 yaml 文件，改完先 `run <名> --dry`
- **门禁是 state 层事实**：等待中的门禁把该篇在所有流水线（含计划任务的 default）里挡住；approve 会校验文件指纹，变了就重置重批

## 故障处置 runbook

| 现象（toast/事件流） | 动作 |
|---|---|
| 🔑 Cookie 失效，队列暂停 | 让用户到引擎项目跑 `publish.py login`；之后条目自愈回 pending |
| ✗ 机审驳回：slug | **绝不自动重发**。读引擎 audit_guard 提示 → 实质重写（换标题+全文重措辞+结构重排）→ 用户新文件入 plan（rewrite_of 指旧 slug）→ 隔天错峰 |
| ⚠ 知乎结果未知转 held | 下个 tick 自动核验 `/p/<id>`；连续核验失败读 `_state/events.jsonl` 排查路由漂移 |
| ⏱ 超时未过审转 held | 人工到创作者中心查；正常过审则改回 |
| ⚠ 对账不一致 | 平台找不到 state 记录的标题——确认是否被手工删除，改 state 或 closed |

事件流 `_state/events.jsonl` 是排障第一现场（append-only，含每次发布/驳回/护栏跳过）。
