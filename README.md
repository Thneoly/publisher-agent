# publisher-agent

掘金 + 知乎双平台**定时发布 Agent**：用户只声明「哪些文章、什么顺序、发哪个平台」，agent 负责排期执行、审核监控、故障响应。本地核心（零 token 跑 happy path），Tauri 桌面壳留作 v2。

```
schtasks（错过自动补触发）
 └─ driver.py tick（纯 Python，唯一外发写者）
     ├─ 子进程契约调用 juejin-publisher（原 skill 一行不改，pin v1.0.6）
     ├─ plan.yaml（你的排期声明）+ _state/（状态/审计/心跳）
     ├─ 护栏：每日上限 / 最小间隔 / 标题双表去重 / 知乎晚于掘金 ≥24h
     └─ 故障协议：驳回→冻结+toast（永不自动重发）；知乎结果未知→held+核验；
        Cookie 401→暂停队列；Windows toast 全程告警
```

## 快速开始

```bash
# 1. 引擎：先装好 juejin-publisher 并 login（本仓库 agent.yaml 指向它的位置）
# 2. 排期：编辑 plan.yaml（见 plan.example.yaml；或让 Claude 跑 /publish-ops 对话生成）
# 3. 自检 + 干跑（不真发）：
uv run python -m publisher_agent.driver doctor
uv run python -m publisher_agent.driver tick --dry
# 4. 上线（注册计划任务，需人工确认）：
powershell Register-ScheduledTask ...   # 见 skills/publish-ops/SKILL.md
```

## 设计红线

- **外发动作唯一写者是 driver**，斜杠命令只读写 plan
- **rejected 永不自动重投**（平台规则：相似内容重发秒拒）——重写走 `rewrite_of` 新条目 + 人工门禁
- **发布时刻冻结标题+sha256**——之后改文件/改标题不触发任何重发
- **知乎「结果未知」≠ 失败**——held + 下个 tick 用 `/p/<id>` 只读核验，杜绝双发
- 队列发完 **disable 而非删除** 计划任务（防静默死亡）

设计全档：`_wf/publish_agent_设计_v2/v3.md`（含红队 10 条意见）；事件流 `_state/events.jsonl` 是未来 Tauri 壳的数据源。

## 伦理边界

依赖逆向接口与 CDP 自动化，仅供个人内容发布；遵守发布纪律（每日 1~2 篇），勿批量营销号式发布。
