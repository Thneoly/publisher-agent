export const meta = {
  name: 'publish-workflow',
  description: '发布编排：预检→干跑→门禁报告→（人工确认）→发布→事后核验。复用 driver 确定性命令面，LLM 只做判断/解读/起草。',
  phases: [
    { title: '预检', detail: 'doctor + 队列/门禁状态' },
    { title: '干跑', detail: 'run <pipeline> --dry（与实跑同名流水线）' },
    { title: '门禁报告', detail: 'gate-report.md：投影+队列+升级项，停下等人' },
    { title: '发布', detail: 'args.confirmed 才执行 run <pipeline>' },
    { title: '事后', detail: 'publish.* 落账核验（不等机审终态）' },
  ],
}

const REPORT = { type: 'object', required: ['summary'], properties: { summary: { type: 'string' } } }
const VERDICT = { type: 'object', required: ['go', 'reason'], properties: { go: { type: 'boolean' }, reason: { type: 'string' }, detail: { type: 'string' } } }

const PIPELINE = args.pipeline || 'default'

// ① 预检：doctor 红项即止（确定性命令，agent 只执行与判读）
const pre = await agent(
  `在 ${args.workdir} 执行（Bash 工具）：
uv run python -m publisher_agent.driver doctor
uv run python -m publisher_agent.driver status
判断：引擎/Cookie/文件是否就绪、有无等待中门禁、队列还剩什么。红项（✗）直接在 summary 写明并标 go=false。`,
  { label: 'pre-check', phase: '预检', schema: VERDICT })

if (!pre.go) return { stopped: '预检未过', reason: pre.reason }

// ② 干跑：与实跑同名流水线（红队：防双引擎分叉）；注意 dry 也会推进对账/审核态
const dry = await agent(
  `在 ${args.workdir} 执行：uv run python -m publisher_agent.driver run ${PIPELINE} --dry
解读输出：各步骤结果、选中了哪篇哪个平台、护栏/门禁拦截原因。summary 里必须注明流水线名 ${PIPELINE}，并提醒「dry 已推进的对账/审核状态变更」。`,
  { label: 'dry-run', phase: '干跑', schema: REPORT })

// ③ 门禁报告：写 gate-report.md，无 confirmed 即停（人工门禁在 workflow 层）
await agent(
  `把干跑结果整理成发布门禁报告，写入 ${args.workdir}/_wf/publish/${args.slug}/gate-report.md（建目录）：
流水线名与步骤投影、将发布的条目（slug/标题/平台）、护栏状态、遗留风险清单（从干跑输出提取）、
「确认后如何继续」一行：重入 workflow 并传 args.confirmed=true。然后用 Read 工具核对文件已写入。`,
  { label: 'gate-report', phase: '门禁报告', schema: REPORT })

if (!args.confirmed) {
  return { needConfirm: true, pipeline: PIPELINE, dry: dry.summary,
           how: `重入本 workflow，args 增加 confirmed: true（其余不变）` }
}

// ④ 发布：执行同名流水线（外发唯一写者仍是 driver）
const pub = await agent(
  `在 ${args.workdir} 执行：uv run python -m publisher_agent.driver run ${PIPELINE}
如实汇总输出（发布成功/护栏跳过/门禁等待/失败分支），不得自行重试或调用引擎 publish.py。`,
  { label: 'publish', phase: '发布', schema: REPORT })

// ⑤ 事后：只核验落账（publish.* 事件 + 条目状态），不等机审终态（终态归 schtasks 轮次的 poll）
const post = await agent(
  `在 ${args.workdir} 执行：uv run python -m publisher_agent.driver status --json
并 Read ${args.workdir}/_state/events.jsonl 的最后 40 行。核验：本次发布的条目处于 in_review/live/held 之一
且对应 publish.* 事件已落账。若出现 rejected/held：给出诊断（结合 events 里 kind）与 rewrite_of 处置建议
（新 slug/换标题/隔天），但只写进报告，不执行。结果写 ${args.workdir}/_wf/publish/${args.slug}/post-report.md。`,
  { label: 'post-verify', phase: '事后', schema: REPORT })

return { pipeline: PIPELINE, published: pub.summary, post: post.summary }
