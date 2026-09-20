# publisher-agent 可编排化施工方案（engine / claude / board 三层合一）

## 0. 分层与统一数据契约

- engine（地基）：pipeline.yaml + driver，唯一外发写者，lockfile 串行，全确定性 Python。
- claude（编排）：publish-workflow.js 只做判断、起草、串链；外发/回写/护栏/对账永远下沉 driver。
- board（呈现）：Tauri 只读 + 进程管理，不含业务判断。

契约＝events.jsonl 唯一事实流：run 开头写 `pipeline.start{run_id,name}`；每步写 `step.begin/end/skip{step,reason}`；既有事件统一补 `step`、`run_id` 字段（`state.event()` 本就透传 kv，各调用点加参即可）；`pub_state.json` 新增 `gates` 段，事件仍 append-only。三文件分工：plan.yaml 答「发什么」（每批重写、发完归档），pipeline.yaml 答「怎么发」（跨批次存活、改动即流程变更、单独进 git 评审），agent.yaml 是站点事实——pipeline 独立第三文件，不并入 plan。生产侧门禁（draft 过人工审核才入队）与发布侧 gate 是两道门，不合并，但事件同流，看板可全链路可视化。

## 1. pipeline.yaml：schema 与两个示例

```yaml
name: default              # 供 run <name> 引用
steps:
  - <step>: {…}            # reconcile/poll/verify-unknown/select/publish/
                            # notify/status/report/doctor/disable-tasks/gate
  # 条目级原语：when: dry|queue_done|picked（闭集谓词，为真才执行）
  #            on-fail: continue|abort|hold
  #            gate: {id, timeout_h}（on-timeout 恒 hold，永不自动放行）
```

默认流水线（无 yaml 时的内置常量，与现 `_tick` 逐字等价）：

```yaml
name: default
steps:
  - reconcile: {daily: true, on-fail: continue}
  - poll: {}
  - verify-unknown: {}
  - select: {platform: juejin}
  - publish: {platform: juejin, on-fail: continue}
  - select: {platform: zhihu}
  - publish: {platform: zhihu, on-fail: continue}
  - disable-tasks: {when: queue_done}
```

进阶（`run reviewed --dry`：publish 跳过、gate 视为放行）：

```yaml
name: reviewed
steps:
  - doctor: {strict: true, on-fail: abort}
  - reconcile: {daily: true, on-fail: continue}
  - poll: {}
  - verify-unknown: {}
  - select: {platform: juejin}
  - gate: {id: pre-publish, timeout_h: 24}
  - publish: {platform: juejin}
  - notify: {}
  - report: {}
```

步骤＝`_tick` 内五段原样抽成注册表 STEPS，签名 `fn(ctx)->StepResult`，ctx 含 cfg/plan/state/adapter/dry/picked/titles_by_platform/run_id；publish 八种结果分支在步骤内消化不外抛；不做 parallel——「唯一外发写者＋lockfile 串行」是确定性根基。

## 2. driver 改造点

1. 新建 steps.py：`_tick`（driver.py:99）五段搬为注册表函数，语义零改动。
2. 新建 pipeline.py：加载 yaml/内置默认；run() 按 when→执行→on-fail 推进。
3. gate＝state 状态机：`gates:{id:{status:waiting|approved|rejected,slug,ts}}`；waiting 时写 gate.wait 事件＋toast 后本次正常收尾（exit 0、幂等）；schtasks 下个 tick 重进仍 waiting 则跳过其后步骤；超时转 held。
4. `state.event` 各调用点补 step/run_id（签名不变）。
5. main()（driver.py:338）choices 增 run/gate：`run [name] [--dry] [--pipeline p]`、`gate approve|reject <id>`（过 lockfile）；tick 保留为 `run default` 别名，schtasks 与 tick.bat 零改动；--dry 时 publish 写 dry.publish-skipped、gate 记 dry.gate-auto。
6. 验收：默认流水线与旧 `_tick` 的 events 逐条 A/B 一致后删 `_tick`。

## 3. publish-workflow.js 骨架

与 article-pipeline.js 同模式（meta.phases＋phase/log/agent/exec，确定性命令走无 LLM 的 exec 直跑 driver，同一条 tick 代码路径，不另造逻辑）。阶段：produce（可选，agent 复用 article-pipeline 全流程，verify_pass=true 才放行）→ enroll（agent 校验 draft 的 frontmatter 与双平台标题，起草 plan 追加条目，含 after:juejin 与 rewrite_of，只写草案不落盘）→ 预检（exec doctor＋status --json，红项即止）→ 干跑（exec tick --dry，agent 解读投影成干跑报告）→ gate（暂停点，汇 gate-report.md＝plan diff＋干跑投影＋升级清单；无 args.confirmed 时 run 到此为止，人点头后带 confirmed 重入）→ 发布（exec tick）→ post（agent 轮询 status --json 至终态；rejected/held 出诊断＋处置建议＋rewrite_of 新条目草案：新 slug、换标题、错峰，落地前必须人确认）。args：slug*、draft、produce?、workdir*、confirmed?、platforms?；产物在 `_wf/publish/<slug>/`（gate-report.md、post-report.md、plan.patch.yaml）。红线：LLM 只做判断与起草，外发、状态回写、护栏、对账永远只归 driver 的确定性 Python；agent 禁触 publish.py、pub_state.json、events.jsonl；publish-ops 仍是运维命令面与 runbook 权威，SKILL.md 加一行分流。决策规则：要判断、起草、串全链路用 workflow；只是查状态、改排期、暂停、救火直接 driver/publish-ops。

## 4. 看板 v1 改动点

做：①步骤条——get_dashboard 已回传事件流，按 run_id 过滤渲染绿/灰/红（灰悬停看 reason），流水线名取自 pipeline.start；②gate 卡片——run_driver 白名单（main.rs:89）追加 run/gate 两项，IPC 面不变；③pipeline.yaml 只读渲染＋「编辑器打开」按钮；④全链路页签——以 slug 为主键合并三个只读来源：`_wf/pipeline/<slug>/`（有目录无 draft.md＝生产中，有 draft.md＝待门禁并显示升级清单条数）、plan 队列、pub_state（in_review/partial/live/rejected），五态一列：生产中→待门禁→排队→审核中→已发/被驳；衔接不引入新协议，靠「进 plan.yaml＝已过生产门禁」这条确定性事实。维持 30s 轮询。

不做：拖拽或看板内编辑 YAML、任何看板回写（Rust 保持只读＋进程管理）、实时推送、单步暂停/续跑控件、多计划下拉。

## 5. 施工顺序与工作量

1. steps 抽取＋A/B 验收（风险最高先做）4h；2. pipeline 加载/when/on-fail＋run 命令＋events 契约 3h；3. gate 状态机＋gate 子命令 3h；4. Tauri 白名单＋gate 卡片＋步骤条＋全链路页签 4h；5. publish-workflow.js（依赖 1–3 的命令面）4h；6. pipeline.example.yaml＋SKILL.md 分流一行 1h。引擎先行，因 workflow 与看板消费同一命令面；每步有独立验收（1：events 逐条一致；2：无 yaml 时回归不变；3：waiting/超时/放行三路径手测；4：旧看板功能不回退）；合计约 19h，六步独立提交、可回滚，1–3 完成即纯 engine 层可用。

## 6. 兼容红线

无 pipeline.yaml 时行为＝现 tick 逐字等价；plan.yaml 结构、state 既有字段、schtasks 任务与命令、Tauri 两个 IPC 命令签名均不动（run_driver 仅白名单追加）；events.jsonl 只加字段不改既有字段名；驳回冻结、知乎未知转 held、401 暂停三铁律原样。

---

# 红队评审（全部采纳）

## 1. [blocker] Gate 在无人值守下是纸门：schtasks 默认流水线绕过，且批准与发布之间载荷不冻结

gate 挂在 pub_state.gates:{id:{status,slug,ts}}，只约束「含 gate 步骤的那条流水线」。而 schtasks/tick.bat 永远跑 tick=run default（无 gate）。场景：用户手动 run reviewed，select 挑中 bian5，gate.wait；下个整点计划任务 run default 照常 select→publish 同一个 bian5——门禁对唯一无值守的发布路径零约束，lockfile 只串行不互斥语义。更深一层：gate.approve 发生在后续进程里，届时 select 会重跑——若 plan.yaml 被改或护栏窗口变化，实际发布的可能不是 gate.wait 时呈现给人的条目；且 gate 只记 slug 不冻结 title/sha256，人批准后文件被改（换标题/改正文）也照发不误。修法三件套：① eligible()（driver.py:60）加第五关——任何带 waiting gate 的 slug 在所有流水线里都不可选，把 gate 从步骤层控制流升级为 state 层事实，default 流水线自然被挡住；② gate.wait 时冻结 {slug,title,sha}（fingerprint 现成），approve 后 publish 前重算指纹，不一致写 gate.stale 并回 waiting；③ 兜底：run <gated> 启动时先 disable cfg.tasks（复用 queue-done 机制，driver.py:233），gate 到终态再 enable。三选一可解，①+② 最符合「确定性优先」。

## 2. [blocker] 双引擎分叉：workflow 干跑/发布 exec 的都是 tick（默认流水线），pipeline.yaml 的 reviewed+gate 永不执行

方案 §3 自己写明：干跑＝exec tick --dry、发布＝exec tick——全是 default 流水线。人基于默认流水线的投影（双平台、无 doctor、无 gate）点头，实跑的也是 default；则 reviewed/gate/notify/report 这条 yaml 全程无人调用，是展示品。反过来人若真手动 run reviewed，又撞上上一条的绕行洞。另有次生漂移：--dry 并非只读——poll 会推进审核态、reconcile 会回填 state（driver.py:100-146 在 dry 分支之前），干跑之后真实 tick 看到的状态已变，gate-report 的「投影」与实跑不严格可比。修法：① workflow 的干跑/发布必须显式 run <name> [--dry] 且两次同名——dry 报告头部写 pipeline 名，confirmed 重入时先校验同名再执行；② 职责收敛为：workflow 只做 produce/enroll/gate 汇报/post 诊断，发布序列（reconcile→poll→…→publish）只存在于 pipeline.yaml 一处，预检＝exec doctor 直调，不复制任何序列语义；③ gate-report 里注明「dry 已推进的 state 变更」清单，消除假投影。

## 3. [major] 过度设计：doctor/status/report/notify 进步骤注册表、when:dry 谓词——编排面膨胀为伪需求

这四类是拉取型运维命令或既有的发布副路径（notify 本就内嵌在 publish 成功路径里），不是「发布流程的阶段」。做成步骤的直接后果：reviewed 示例里 report 的 stdout 死在无人值守的 tick.log，没有任何消费者；status 同理；而每个步骤类型×when×on-fail×gate 的组合都要进验收矩阵，schema 与测试面无谓翻倍——恰是用户反对的复杂化。when 闭集里的 dry 也是伪谓词：--dry 已是全局语义（publish 跳过即投影），不存在需要 when:dry 的真实场景。用户要编排的是发布流程，不是任意命令序列。修法：注册表收敛到「会写 state 或外发」的原语七种——reconcile/poll/verify-unknown/select/publish/disable-tasks/gate；doctor 留给 workflow 预检 exec 直调（现设计已是）；notify 保持内嵌于 publish 成功路径不动；status/report 永不进步骤表；when 只留 queue_done/picked（picked 恰是 gate 的前置守卫），dry 删除。

## 4. [major] blog 侧真正入口 skill 未改道：fde-pipeline 与 article-pipeline 仍指 publish.py 直发，第三条发布路径存活

施工清单对生产侧只有「SKILL.md 加一行分流」，且加在 publisher-agent 的 publish-ops 上。但用户日常触发的是 blog 仓库的 fde-pipeline（触发词「写新文章/发布文章」），其 ③ 发布 段明文 uv run publish.py publish/zhihu --auto；article-pipeline 的 return.next 也写「uv run publish.py preview/publish」。这条路完全绕过 driver/state/护栏/标题冻结——「生产→发布全链路可编排」在最常用的入口处断链，且制造第二真相源（平台事实 vs pub_state）。修法：fde-pipeline 的 ③ 发布段与 article-pipeline 的 next 改指 publish-workflow（enroll 起草入队→人确认→driver 发布）；publish.py 直发只保留 runbook 救火语义并加禁令标注。这是 blog 仓库的真实改动，必须列入施工第 5 步的验收项，不是一行分流能覆盖。

## 5. [major] 「IPC 面不变」自相矛盾：gate 卡片/带参子命令在看板没有数据通路，步骤事件还会刷爆 30 条窗口

三个具体落空：(a) main.rs 的 run_driver(cmd,dry) 是整串白名单 match，无法表达 run reviewed / gate approve pre-publish 这类带位置参数的子命令——要么改签名加 args 向量、要么把整串塞进 cmd 破坏白名单匹配，两者都是 IPC 变更，「仅白名单追加、签名不动」的承诺不成立。(b) get_dashboard 只回 status 行/事件尾 30/heartbeat/plan_raw，没有 gates 段也没有 pipeline 名，gate 卡片与步骤条无米下锅。(c) 每步 step.begin/end/skip 使一次 tick 产生 20+ 行事件（8 步×2~3 条），publish.ok/audit.rejected 这类实质事件会被挤出 tail 30，排障第一现场反而被编排自身淹没。修法：认下一次 IPC 变更——status --json 扩为 {rows,gates,pipeline}（维持「命令面即 IPC」，Rust 不直读 pub_state.json），run_driver 增加 args 数组参数；get_dashboard 读扩展后的 status；事件流按类型白名单过滤（步骤事件折叠进步骤条单独查询），实质事件优先。

## 6. [major] plan.yaml 出现第三个写者后，坏计划让计划任务裸崩：无 toast、无事件、队列静默停摆

main()（driver.py:338）里 Config.load/Plan() 抛异常会带崩整个 tick：schtasks 只留 tick.log 一行栈，没有任何告警。目前写者是人+publish-ops 尚可控；enroll 之后 plan.yaml 写者变成三个（人/publish-ops/workflow 草案落地），而 Plan.__init__ 对 slug 重复、queue 结构错误是加载期异常，frontmatter 缺标题则要拖到发布时刻才暴露——无人值守系统的死亡方式恰恰不该是静默的。修法：① driver 入口包 try/except：配置/计划加载失败 → toast+event(plan.load-failed)+exit 2，把静默死亡变成显式告警（这是今天就该修的存量问题，编排化放大它）；② enroll 落盘前跑 driver doctor 并给 doctor 补 slug 重复/queue 结构检查（或新增 plan-validate），红灯不落盘；③ workflow 落地 plan.patch 后立即跑一次 run <name> --dry 复核。

## 7. [major] 全链路五态的「有 draft.md＝待门禁」判定必然误报：article-pipeline 初稿阶段就写 draft.md

方案 §4 用目录形状猜状态：「_wf/pipeline/<slug>/ 有目录无 draft.md＝生产中，有 draft.md＝待门禁」。核查 article-pipeline.js:155——draft.md 在初稿阶段即写入，其后三路评审、修订、复审全部原地编辑同一文件。即「有 draft.md」是生产周期的常态而非完成的标志，五态页签会在数小时的生产过程中一直显示「待门禁」，五态链的第一环就是错的。修法：不要按目录形状推断——article-pipeline 收尾（verify_pass 且升级清单产出后）落一个显式终态标记（如 _wf/pipeline/<slug>/READY.md，或复用 _wf/workflowProgress.json 已有的 phase 字段），全链路页签读标记判定「待门禁」；「进 plan.yaml＝已过生产门禁」这条确定性衔接保持不变。

## 8. [minor] post 阶段「轮询 status 至终态」与平台机审时间尺度错配：workflow 会话不该等 6~24 小时

juejin 机审以小时计（driver 的慢审通知阈值 6h、转 held 24h，driver.py:33-34），方案却让 publish-workflow 的 post 阶段 agent 轮询 status --json 到终态——要么 workflow 会话挂着几小时空转烧 token 占用会话，要么中途放弃得到「post 无结论」；知乎 unknown 的核验同样要等下个 tick。修法：post 阶段只核验到「本次 run_id 的 publish.* 已落账且条目处于 in_review/held/live 之一」即收尾出 post-report；终态监测本就是 schtasks poll+toast 的职责（现有机制零改动），rejected/held 的事后诊断做成 publish-workflow 的独立重入（post-diagnose）或走 publish-ops runbook，不绑架发布会话。

