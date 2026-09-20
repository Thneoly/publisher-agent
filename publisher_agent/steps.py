# -*- coding: utf-8 -*-
"""步骤原语注册表（红队收敛：只收「会写 state 或外发」的七种）。

reconcile / poll / verify-unknown / select / publish / disable-tasks / gate
每个原语 fn(ctx, params) -> StepResult；语义从旧 _tick 原样迁移，零改动。
doctor/status/report/notify 不进步骤表——它们是运维命令或发布内嵌副路径。
"""
from __future__ import annotations

import re
import subprocess
import time
from datetime import datetime

from .notify import toast
from . import verifier

REVIEW_TIMEOUT_NOTIFY_H = 6
REVIEW_TIMEOUT_HELD_H = 24
ZHIHU_AFTER_H = 24
TERMINAL = ("live", "closed", "held", "rejected")


class Ctx:
    """流水线全程共享上下文。"""

    def __init__(self, cfg, plan, state, adapter, dry: bool, run_id: str, name: str):
        self.cfg, self.plan, self.state, self.adapter = cfg, plan, state, adapter
        self.dry, self.run_id, self.name = dry, run_id, name
        self.picked: dict = {}          # platform -> plan.Item
        self.titles: dict = {}          # platform -> set[str]
        self.queue_done: bool = False
        self.step: str = ""             # 当前步骤名（事件字段）

    def ev(self, type_: str, **kv) -> None:
        self.state.event(type_, step=self.step, run_id=self.run_id, **kv)

    def log(self, msg: str) -> None:
        print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


class StepResult:
    def __init__(self, status: str, reason: str = ""):
        self.status, self.reason = status, reason    # ok / skip / fail / gate_wait


# ------------------------------------------------------------------ 护栏与选篇

def guard_ok(state, platform: str, cad: dict) -> str | None:
    """per_day 缺省/0 = 不设每日上限；只保留间隔（数量由队列与间隔决定）。"""
    per_day = int(cad.get("per_day") or 0)
    min_gap = float(cad.get("min_gap_h", 1))
    if per_day:
        n = state.published_today(platform)
        if n >= per_day:
            return f"今日 {platform} 已发 {n}/{per_day}"
    last = state.last_publish_ts(platform)
    if last and (time.time() - last) / 3600 < min_gap:
        return f"距上一篇仅 {(time.time() - last) / 3600:.1f}h < {min_gap}h"
    return None


def eligible(plan, item, platform: str, state, cfg, platform_titles: set) -> tuple[bool, str]:
    st = state.status(item.id)
    if st in TERMINAL:
        return False, f"状态 {st}"
    spec = item.platforms.get(platform)
    if spec is None:
        return False, "不投此平台"
    gid = state.slug_gated(item.id)                       # 红队 blocker：waiting gate 挡一切流水线
    if gid:
        return False, f"门禁 {gid} 等待中"
    after = item.platform_after(platform)
    if after:
        dep = state.plat(item.id, after)
        if not dep.get("live"):
            return False, f"等待 {after} 先上线"
        if (time.time() - float(dep.get("live_ts") or 0)) / 3600 < ZHIHU_AFTER_H:
            return False, f"{after} 上线不足 {ZHIHU_AFTER_H}h"
    at = item.platform_at(platform)               # 新排期模型：每篇×每平台可指定绝对时间
    if at and datetime.now() < at:
        return False, f"未到发布时间（{at:%m-%d %H:%M}）"
    fp = plan.post_fingerprint(item, cfg)
    title = fp["title_juejin"] if platform == "juejin" else fp["title_zhihu"]
    if title in platform_titles or title in state.frozen_titles(platform):
        return False, "标题已存在（平台或冻结表）"
    if not title:
        return False, "缺 frontmatter 标题"
    return True, ""


# ------------------------------------------------------------------- 原语

def step_reconcile(ctx: Ctx, p: dict) -> StepResult:
    state, plan, cfg = ctx.state, ctx.plan, ctx.cfg
    today = datetime.now().date().isoformat()
    if state._data.get("last_reconcile") == today and not p.get("force"):
        try:    # 对账降频≠去重降频：发布前仍拉一次平台标题
            ctx.titles["juejin"] = {a["title"] for a in verifier.juejin_recent(ctx.adapter)}
        except Exception:
            ctx.titles["juejin"] = set()
        ctx.titles.setdefault("zhihu", set())
        return StepResult("skip", "今日已对账（仍拉了去重表）")
    try:
        recent = verifier.juejin_recent(ctx.adapter)
    except Exception as e:
        ctx.titles["juejin"], ctx.titles["zhihu"] = set(), set()
        ctx.log(f"⚠ 对账失败（不阻塞发布）：{str(e)[:120]}")
        return StepResult("ok", f"对账失败不阻塞：{str(e)[:60]}")
    live_titles = {a["title"] for a in recent}
    # 回填：pending 条目标题已在平台 → 补 state（幂等；文件改名/状态丢失自愈）
    for item in plan.items:
        if state.plat(item.id, "juejin").get("live"):
            continue
        try:
            fp = plan.post_fingerprint(item, cfg)
        except Exception:
            continue
        match = next((a for a in recent if a["title"] == fp["title_juejin"]), None)
        if match:
            state.set_platform(item.id, "juejin", live=True, live_ts=match["ctime"],
                               published_ts=match["ctime"], title=fp["title_juejin"],
                               sha=fp["sha256"], article_id=match["article_id"],
                               published_at=datetime.fromtimestamp(match["ctime"]).isoformat(timespec="seconds"))
            z_pending = "zhihu" in item.platforms and not state.plat(item.id, "zhihu").get("live")
            state.set_status(item.id, "partial" if z_pending else "live")
            ctx.ev("reconcile.backfilled", slug=item.id)
    # 漂移：state 标 live 但平台找不到标题——只告警，连续 2 天才 held
    for slug, e in list(state._data["entries"].items()):
        j = (e.get("platforms") or {}).get("juejin") or {}
        if e.get("status") in ("live", "partial") and j.get("title") and j["title"] not in live_titles:
            miss = j.get("reconcile_miss", 0) + 1
            j["reconcile_miss"] = miss
            if miss >= 2:
                e["status"] = "held"
                ctx.ev("reconcile.held", slug=slug, title=j["title"])
                toast(f"⚠ 对账不一致转 held：{slug}", "平台找不到该标题，人工核对")
            else:
                ctx.ev("reconcile.mismatch", slug=slug, miss=miss)
    state._data["last_reconcile"] = today
    state.save()
    ctx.titles["juejin"] = live_titles
    ctx.titles.setdefault("zhihu", set())
    return StepResult("ok")


def step_poll(ctx: Ctx, p: dict) -> StepResult:
    state, plan, adapter = ctx.state, ctx.plan, ctx.adapter
    for slug in [s for s, e in state._data["entries"].items() if e.get("status") == "in_review"]:
        aid = state.plat(slug, "juejin").get("article_id")
        if not aid:
            continue
        res = adapter.juejin_status(aid)
        if res == "live":
            state.set_platform(slug, "juejin", live=True, live_ts=time.time())
            z_pending = any(i.id == slug and "zhihu" in i.platforms and
                            not state.plat(slug, "zhihu").get("live") for i in plan.items)
            state.set_status(slug, "partial" if z_pending else "live")
            ctx.ev("audit.passed", slug=slug, article_id=aid)
            toast(f"✓ 已上线：{slug}", f"juejin.cn/post/{aid}")
        elif res == "rejected":
            state.set_status(slug, "rejected")
            ctx.ev("audit.rejected", slug=slug, article_id=aid)
            toast(f"✗ 机审驳回：{slug}", "已冻结，等人工实质重写（rewrite_of 流程）")
        else:
            age = (time.time() - float(state.plat(slug, "juejin").get("published_ts") or 0)) / 3600
            if age > REVIEW_TIMEOUT_HELD_H:
                state.set_status(slug, "held")
                ctx.ev("review.timeout-held", slug=slug, age_h=round(age, 1))
                toast(f"⏱ 超时未过审转 held：{slug}", f"{age:.0f}h，人工到创作者中心查")
            elif age > REVIEW_TIMEOUT_NOTIFY_H and not state.plat(slug, "juejin").get("notified_slow"):
                state.set_platform(slug, "juejin", notified_slow=True)
                toast(f"⏳ 审核超 {REVIEW_TIMEOUT_NOTIFY_H}h：{slug}", "仍在审核中，继续等")
        state.save()
    return StepResult("ok")


def step_verify_unknown(ctx: Ctx, p: dict) -> StepResult:
    state = ctx.state
    for slug in [s for s, e in state._data["entries"].items() if e.get("status") == "held"]:
        z = state.plat(slug, "zhihu")
        url = z.get("url")
        if url and z.get("kind") == "UNKNOWN_RESULT":
            alive = verifier.zhihu_article_alive(url, z.get("title"))
            if alive == "alive":
                state.set_status(slug, "live")
                state.set_platform(slug, "zhihu", live=True, live_ts=time.time())
                ctx.ev("zhihu.unknown-verified-alive", slug=slug)
                toast(f"✓ 核验成功（此前的未知结果实为已发布）：{slug}", url)
                state.save()
            else:
                ctx.ev("zhihu.verify-retry-later", slug=slug, result=alive)
    return StepResult("ok")


def step_select(ctx: Ctx, p: dict) -> StepResult:
    platform = p["platform"]
    cad = {**ctx.cfg.cadence.get(platform, {}), **ctx.plan.cadence.get(platform, {})}
    why = guard_ok(ctx.state, platform, cad)
    if why:
        ctx.log(f"[{platform}] 护栏跳过：{why}")
        return StepResult("skip", why)
    picked = None
    for item in ctx.plan.items:
        ok, reason = eligible(ctx.plan, item, platform, ctx.state, ctx.cfg,
                              ctx.titles.get(platform, set()))
        if ok:
            picked = item
            break
        if ctx.state.status(item.id) in ("pending", "partial") and item.platforms.get(platform) is not None:
            ctx.log(f"[{platform}] {item.id} 未到：{reason}")
    if not picked:
        ctx.log(f"[{platform}] 无可发条目")
        return StepResult("skip", "无可发条目")
    ctx.picked[platform] = picked
    return StepResult("ok", picked.id)


def step_gate(ctx: Ctx, p: dict) -> StepResult:
    """人工门禁 = state 层事实（红队 blocker 修法）：waiting 冻结 {slug,title,sha}，
    批准/超时/陈旧三态迁移；waiting 期间该 slug 在所有流水线不可选。"""
    state = ctx.state
    gid = p["id"]
    gate = state.gate(gid)
    timeout_h = float(p.get("timeout_h", 24))
    target = ctx.picked.get(p.get("platform", "juejin"))
    if not target:
        return StepResult("skip", "无 picked")
    if gate.get("status") == "waiting":
        age_h = (time.time() - float(gate.get("ts") or 0)) / 3600
        if age_h > timeout_h:
            state.set_status(gate.get("slug", ""), "held")
            state.set_gate(gid, status="expired")
            ctx.ev("gate.expired", gate=gid, slug=gate.get("slug"))
            toast(f"⏱ 门禁超时转 held：{gate.get('slug')}", f"gate {gid} {timeout_h}h 未批准")
            return StepResult("ok", "gate expired")
        ctx.log(f"门禁 {gid} 仍在等待人工批准（{age_h:.1f}h/{timeout_h}h）")
        return StepResult("gate_wait", gid)
    if gate.get("status") == "approved" and gate.get("slug") == target.id:
        return StepResult("ok", "approved 放行")
    # 无 gate / 已终态 / 批的不是这篇 → 对当前 picked 建门
    fp = ctx.plan.post_fingerprint(target, ctx.cfg)
    title = fp["title_juejin"] if p.get("platform", "juejin") == "juejin" else fp["title_zhihu"]
    state.set_gate(gid, id=gid, status="waiting", slug=target.id, title=title,
                   sha=fp["sha256"], ts=time.time())
    ctx.ev("gate.wait", gate=gid, slug=target.id, title=title[:40])
    toast(f"🚧 等待人工门禁 {gid}", f"{target.id}《{title[:30]}》—— approve 后下轮发布")
    return StepResult("gate_wait", gid)


def step_publish(ctx: Ctx, p: dict) -> StepResult:
    state, plan, cfg, adapter = ctx.state, ctx.plan, ctx.cfg, ctx.adapter
    platform = p["platform"]
    picked = ctx.picked.get(platform)
    if not picked:
        return StepResult("skip", "无 picked")
    fp = plan.post_fingerprint(picked, cfg)
    title = fp["title_juejin"] if platform == "juejin" else fp["title_zhihu"]

    # 门禁放行校验：waiting/陈旧不放行（红队：批准与发布之间载荷必须冻结）
    gid = state.slug_gated(picked.id)
    if gid:
        state.set_gate(gid, status="waiting")   # 不该到这（eligible 已挡），双保险
        ctx.ev("gate.blocked-publish", gate=gid, slug=picked.id)
        return StepResult("skip", f"门禁 {gid} 等待中")
    for g in state._data.get("gates", {}).values():
        if g.get("slug") == picked.id and g.get("status") == "approved":
            fp2 = plan.post_fingerprint(picked, cfg)
            if fp2["sha256"] != g.get("sha"):
                state.set_gate(g.get("id", ""), status="waiting")
                ctx.ev("gate.stale", slug=picked.id)
                toast(f"⚠ 门禁载荷已变，重新等批准：{picked.id}", "批准后文件被修改")
                return StepResult("skip", "gate stale")

    file_arg = str(picked.file).replace("\\", "/")
    if ctx.dry:
        ctx.log(f"[{platform}] DRY：将发布 {picked.id}（{file_arg}）标题《{title[:20]}》")
        ctx.ev("dry.publish-skipped", slug=picked.id, platform=platform)
        return StepResult("ok", "dry")

    state.set_platform(picked.id, platform, intent_at=time.time(), title=title, sha=fp["sha256"])
    state.save()
    ctx.log(f"[{platform}] 发布 {picked.id}：《{title[:24]}》")
    column = (picked.platforms.get("juejin") or {}).get("column") if platform == "juejin" else None
    r = (adapter.publish_juejin(file_arg, column) if platform == "juejin"
         else adapter.publish_zhihu(file_arg))
    now = time.time()
    if r.ok:
        kv = dict(published_at=datetime.now().isoformat(timespec="seconds"),
                  published_ts=now, title=title, sha=fp["sha256"])
        if platform == "juejin":
            kv.update(article_id=r.juejin_id)
            state.set_platform(picked.id, "juejin", **kv)
            state.set_status(picked.id, "in_review")
        else:
            kv.update(url=r.zhihu_url, live=True, live_ts=now)
            state.set_platform(picked.id, "zhihu", **kv)
            if state.plat(picked.id, "juejin").get("live") or "juejin" not in picked.platforms:
                state.set_status(picked.id, "live")
            else:
                state.set_status(picked.id, "partial")
        ctx.ev("publish.ok", slug=picked.id, platform=platform,
               article_id=r.juejin_id, url=r.zhihu_url)
        toast(f"✓ 已发布[{platform}]：{picked.id}", title[:40])
    else:
        state.set_platform(picked.id, platform, kind=r.kind, last_error=r.output[-300:])
        if r.kind == "UNKNOWN_RESULT":
            state.set_status(picked.id, "held")
            ctx.ev("publish.unknown", slug=picked.id, platform=platform)
            toast(f"⚠ 知乎结果未知转 held：{picked.id}", "下个 tick 自动核验，不重发")
        elif r.kind == "COOKIE_EXPIRED":
            state.set_status(picked.id, "held")
            ctx.ev("publish.cookie-expired", slug=picked.id, platform=platform)
            toast("🔑 Cookie 失效，队列暂停", "扫码重跑引擎 login 后自愈")
        else:
            state.set_status(picked.id, "failed")
            ctx.ev("publish.failed", slug=picked.id, platform=platform, kind=r.kind)
            toast(f"✗ 发布失败[{platform}]：{picked.id}", f"{r.kind}，详见 events.jsonl")
    state.save()
    # 门禁释已放行发布的 gate
    for gkey, g in state._data.get("gates", {}).items():
        if g.get("slug") == picked.id and g.get("status") == "approved":
            state.set_gate(gkey, status="released")
    return StepResult("ok")


def step_disable_tasks(ctx: Ctx, p: dict) -> StepResult:
    if not ctx.plan.items or not all(
            ctx.state.status(i.id) in TERMINAL for i in ctx.plan.items):
        ctx.queue_done = False
        return StepResult("skip", "队列未完")
    ctx.queue_done = True
    for tn in ctx.cfg.tasks:
        subprocess.run(["schtasks", "/Change", "/TN", tn, "/DISABLE"], capture_output=True)
    ctx.ev("queue.done-disabled-tasks", tasks=",".join(ctx.cfg.tasks))
    toast("🎉 队列发完，计划任务已停用", "重新排期时 /publish-ops deploy 再启用")
    return StepResult("ok")


REGISTRY = {
    "reconcile": step_reconcile,
    "poll": step_poll,
    "verify-unknown": step_verify_unknown,
    "select": step_select,
    "gate": step_gate,
    "publish": step_publish,
    "disable-tasks": step_disable_tasks,
}


def compute_queue_done(ctx: Ctx) -> None:
    """when:queue_done 谓词求值（disable-tasks 自身也会判定）。"""
    ctx.queue_done = bool(ctx.plan.items) and all(
        ctx.state.status(i.id) in TERMINAL for i in ctx.plan.items)
