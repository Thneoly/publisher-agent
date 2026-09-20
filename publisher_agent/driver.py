# -*- coding: utf-8 -*-
"""driver：发布 Agent 的执行体（唯一外发写者；计划任务调用，无需 Claude 在线）。

子命令：
  tick [--dry]              = run default（兼容别名；schtasks/tick.bat 零改动）
  run [name] [--dry] [--pipeline <file>]   跑指定流水线（缺省 default）
  gate approve|reject <id>  人工门禁放行/否决
  doctor / status [--json] / report

铁律：rejected 永不自动重投；知乎「结果未知」→ held + 核验；waiting gate 挡所有流水线。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import traceback
from datetime import datetime
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

from .adapter import Adapter
from . import pipeline as PIPE
from .notify import toast
from .plan import Config, Plan, ROOT
from .state import State
from . import verifier


def log(msg: str) -> None:
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------------ run/tick

def run_once(cfg: Config, plan: Plan, state: State, adapter: Adapter,
             name: str, dry: bool, pipeline_file: Path | None) -> int:
    if not state.acquire():
        log("另一 tick 在跑（lockfile），本次退出")
        return 0
    try:
        pl = PIPE.load(name, pipeline_file)
        result = PIPE.run(pl, cfg, plan, state, adapter, dry)
        return 0 if result in ("ended", "gate_wait") else 1
    finally:
        state.release()
        state.touch_heartbeat()


def gate_cmd(cfg: Config, plan: Plan, state: State, action: str, gate_id: str) -> int:
    g = state.gate(gate_id)
    if not g or g.get("status") != "waiting":
        log(f"门禁 {gate_id} 不存在或不在等待态（当前 {g.get('status', '无')}）")
        return 1
    if action == "approve":
        # 红队：批准前校验载荷未变（文件指纹 vs 建门时冻结值）
        item = next((i for i in plan.items if i.id == g.get("slug")), None)
        if item:
            fp = plan.post_fingerprint(item, cfg)
            if fp["sha256"] != g.get("sha"):
                state.set_gate(gate_id, status="waiting", sha=fp["sha256"], ts=time.time())
                state.event("gate.stale-rejected-approval", gate=gate_id, slug=g.get("slug"))
                toast(f"⚠ 批准被拒：文件已变化", f"{g.get('slug')} 门禁重置，请重新确认")
                log("文件指纹与建门时不一致——门禁已重置，重新核对后再 approve")
                return 1
        state.set_gate(gate_id, status="approved", approved_ts=time.time())
        state.event("gate.approved", gate=gate_id, slug=g.get("slug"))
        toast(f"✅ 门禁放行：{gate_id}", f"{g.get('slug')} 下轮流水线将发布")
        return 0
    state.set_gate(gate_id, status="rejected", rejected_ts=time.time())
    state.event("gate.rejected", gate=gate_id, slug=g.get("slug"))
    toast(f"🚫 门禁否决：{gate_id}", f"{g.get('slug')} 保持排队，可改流水线或改稿")
    return 0


# ------------------------------------------------------------------ 只读命令

def doctor(cfg: Config, plan: Plan, state: State, adapter: Adapter) -> int:
    ok = True
    print(f"引擎脚本：{cfg.engine_script}  {'✓' if Path(cfg.engine_script).exists() else '✗ 不存在'}")
    ok &= Path(cfg.engine_script).exists()
    env = cfg.engine_cwd / ".juejin.env"
    print(f"掘金 Cookie 文件：{env}  {'✓' if env.exists() else '✗ 缺（跑引擎 login）'}")
    ok &= env.exists()
    if env.exists():
        w = adapter.whoami()
        valid = "Cookie 有效" in w.output
        name = ""
        if valid:
            import re as _re
            m = _re.search(r"Cookie 有效[：:]\s*(\S+?)（", w.output)
            name = m.group(1) if m else ""
        print(f"掘金登录态：{'✓ 有效' + (f'（{name}）' if name else '') if valid else '✗ 失效——工作台「扫码登录」重扫'}")
        ok &= valid
    print(f"CDP 9222：{'✓ 活着' if adapter.verify_cdp_alive() else '✗ 未启动（zhihu 依赖；掘金不需要）'}")
    for i in plan.items:
        f = plan.resolve_file(i, cfg)
        if not f.exists():
            print(f"  ✗ {i.id} 文件缺失：{f}")
            ok = False
    print(f"计划：{len(plan.items)} 条；状态：{len(state._data['entries'])} 条；"
          f"等待中门禁：{len(state.gates_waiting())}")
    print(f"流水线：default（内置）+ pipelines/*.yaml = "
          f"{len(list((ROOT / 'pipelines').glob('*.yaml'))) if (ROOT / 'pipelines').exists() else 0} 条自定义")
    print("自检" + ("通过 ✓" if ok else "有问题 ✗"))
    return 0 if ok else 1


def status(cfg: Config, plan: Plan, state: State, as_json: bool = False) -> int:
    rows = []
    for i in plan.items:
        e = state.entry(i.id)
        rows.append({
            "slug": i.id, "status": e.get("status"),
            "file": str(i.file),
            "juejin_at": (i.platforms.get("juejin") or {}).get("at", ""),
            "zhihu_at": (i.platforms.get("zhihu") or {}).get("at", ""),
            "juejin": _plat_brief(e, "juejin"), "zhihu": _plat_brief(e, "zhihu"),
        })
    if as_json:
        # 看板数据契约（红队：gates 与流水线信息走同一 IPC，Rust 不直读 state）
        print(json.dumps({
            "rows": rows,
            "gates": state._data.get("gates", {}),
            "pipeline": state._data.get("last_pipeline", {}),
            "heartbeat_task": _heartbeat_task_state(),
        }, ensure_ascii=False, indent=1))
        return 0
    for r in rows:
        print(f"{r['slug']:<14} {str(r['status']):<10} juejin:{r['juejin']:<28} zhihu:{r['zhihu']}")
    for gid, g in state.gates_waiting().items():
        print(f"🚧 门禁 {gid} 等待中：{g.get('slug')}《{str(g.get('title'))[:30]}》")
    return 0


def _heartbeat_task_state() -> str:
    """BlogAgent_hourly 状态：running / disabled / missing（编排页据此提示要不要 deploy）。"""
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", "BlogAgent_hourly", "/FO", "LIST"],
                       capture_output=True)
    if r.returncode != 0:
        return "missing"
    text = (r.stdout or b"").decode("gbk", "replace")
    return "disabled" if ("已禁用" in text or "Disabled" in text) else "running"


def _plat_brief(e: dict, p: str) -> str:
    d = (e.get("platforms") or {}).get(p)
    if not d:
        return "—"
    if d.get("live"):
        return f"live {str(d.get('url') or d.get('article_id'))[-24:]}"
    if d.get("article_id") and p == "juejin":
        return f"in_review {d['article_id'][-12:]}"
    if d.get("url") and p == "zhihu":
        return f"unknown {d['url'][-16:]}"
    return d.get("kind", "pending")[:24]


def report(cfg: Config, plan: Plan, state: State) -> int:
    print(f"# 发布台账 {datetime.now():%Y-%m-%d %H:%M}\n")
    status(cfg, plan, state)
    hb = state.heartbeat.read_text(encoding="utf-8") if state.heartbeat.exists() else "无"
    lp = state._data.get("last_pipeline", {})
    print(f"\n心跳：{hb}｜上次流水线：{lp.get('name', '—')} @ {lp.get('ts', '—')}")
    return 0


# ------------------------------------------------------------ 排期命令（新模型）

def plan_set(cfg: Config, json_path: Path, plan_path: Path) -> int:
    """UI 的编排结果落盘：JSON 规格 → 校验 → 原子写 plan.yaml + 事件。
    JSON：{"cadence": {...}, "queue": [{"id","file","juejin":{"at":...},"zhihu":{"after","at"}}]}"""
    import yaml
    spec = json.loads(json_path.read_text(encoding="utf-8"))
    queue = spec.get("queue") or []
    if not queue:
        print("✗ 队列为空，拒绝写入", file=sys.stderr)
        return 1
    seen = set()
    for e in queue:
        if not e.get("id") or not e.get("file"):
            print(f"✗ 条目缺 id/file：{e}", file=sys.stderr)
            return 1
        if e["id"] in seen:
            print(f"✗ slug 重复：{e['id']}", file=sys.stderr)
            return 1
        seen.add(e["id"])
        f = Path(e["file"])
        if not (f if f.is_absolute() else cfg.engine_cwd / f).exists():
            print(f"✗ 文件不存在：{e['file']}", file=sys.stderr)
            return 1
        for pf in ("juejin", "zhihu"):
            spec_pf = e.get(pf) or {}
            if spec_pf.get("at"):
                try:
                    from datetime import datetime as _dt
                    _dt.strptime(str(spec_pf["at"]), "%Y-%m-%d %H:%M")
                except ValueError:
                    print(f"✗ at 时间格式应为 'YYYY-MM-DD HH:MM'：{e['id']}.{pf}={spec_pf['at']}", file=sys.stderr)
                    return 1
    doc = {"cadence": spec.get("cadence") or {"juejin": {"min_gap_h": 1}, "zhihu": {"min_gap_h": 25}},
           "queue": queue}
    tmp = plan_path.with_suffix(".tmp")
    tmp.write_text(yaml.safe_dump(doc, allow_unicode=True, sort_keys=False), encoding="utf-8")
    import os
    os.replace(tmp, plan_path)
    State(cfg.state_dir).event("plan.updated", items=len(queue))
    toast("📅 发布计划已更新", f"{len(queue)} 条入队；deploy 确保每小时心跳在跑")
    print(f"✓ plan.yaml 已更新（{len(queue)} 条）——记得 deploy 确保心跳任务在跑")
    return 0


def deploy(cfg: Config) -> int:
    """注册唯一一个每小时心跳任务（任意 at 时间点由此触发，改排期不用动任务）。幂等。"""
    import subprocess
    r = subprocess.run(["schtasks", "/Query", "/TN", "BlogAgent_hourly"], capture_output=True)
    if r.returncode == 0:
        print("✓ 心跳任务 BlogAgent_hourly 已存在")
        return 0
    bat = ROOT / "tick.bat"
    ps = (
        "$a = New-ScheduledTaskAction -Execute '" + str(bat) + "'; "
        "$t = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(2) "
        "-RepetitionInterval (New-TimeSpan -Hours 1); "
        "$s = New-ScheduledTaskSettingsSet -StartWhenAvailable; "
        "Register-ScheduledTask -TaskName BlogAgent_hourly -Action $a -Trigger $t -Settings $s | Out-Null"
    )
    r2 = subprocess.run(["powershell", "-NoProfile", "-Command", ps], capture_output=True)
    ok = r2.returncode == 0
    State(cfg.state_dir).event("deploy.hourly", ok=ok)
    print(("✓ 已注册 BlogAgent_hourly（每小时心跳，StartWhenAvailable 错过补跑）" if ok
           else f"✗ 注册失败：{r2.stderr.decode('utf-8', 'replace')[:200]}"))
    return 0 if ok else 1


def login_cmd(cfg: Config, platform: str) -> int:
    """内嵌 Chromium 扫码登录（掘金 Cookie 喂给引擎 .juejin.env；知乎存应用自有 profile）。"""
    from . import browser
    ok_all = True
    try:
        if platform in ("juejin", "both"):
            print("· 掘金扫码中……（内嵌 Chromium 窗口，最长 5 分钟）", flush=True)
            ok = browser.login_juejin(cfg.engine_cwd / ".juejin.env")
            print("✓ 掘金 Cookie 已写入引擎 .juejin.env" if ok else "✗ 掘金登录超时", flush=True)
            ok_all &= ok
        if platform in ("zhihu", "both"):
            print("· 知乎扫码中……", flush=True)
            ok = browser.login_zhihu()
            print("✓ 知乎登录态已存内嵌 profile" if ok else "✗ 知乎登录超时", flush=True)
            ok_all &= ok
    finally:
        browser.shutdown()          # 不关干净：进程挂住不退出，调用方永远等
    return 0 if ok_all else 1


def scan(cfg: Config, target: str) -> int:
    """内容源发现：文件夹或单文件 → 文章清单 JSON（供工作台「内容源」页）。"""
    from .browser import _frontmatter
    p = Path(target)
    # 递归扫描：D:\Blogs\<专栏>\xxx.md 这类结构也能一次全发现（file 带相对子目录）
    files = [p] if p.is_file() else sorted(p.rglob("*.md"))
    rows = []
    for f in files:
        try:
            fm = _frontmatter(f)
        except Exception:
            continue
        rows.append({
            "file": str(f.resolve()),
            "name": f.name,
            "folder": f.parent.name,      # 专栏匹配键：父文件夹名（D:\Blogs\<专栏>\x.md）
            "title_juejin": fm["fields"].get("title_juejin", ""),
            "title_zhihu": fm["fields"].get("title_zhihu", ""),
            "description": fm["fields"].get("description", "")[:60],
            "tags": fm["fields"].get("tags", ""),
            "chars": len(fm["body"]),
        })
    print(json.dumps(rows, ensure_ascii=False))
    return 0


def undeploy(cfg: Config) -> int:
    """停用心跳（不删，红队裁定；计划/队列/状态全保留，随时 deploy 再启用）。"""
    import subprocess
    r = subprocess.run(["schtasks", "/Change", "/TN", "BlogAgent_hourly", "/DISABLE"],
                       capture_output=True)
    ok = r.returncode == 0
    State(cfg.state_dir).event("deploy.undeploy", ok=ok)
    print("✓ 心跳已停用（计划与状态都保留，随时重新 deploy 启用）" if ok
          else "✗ 停用失败（BlogAgent_hourly 不存在？）")
    if ok:
        toast("⏸ 每小时心跳已停用", "计划保留；重新部署即恢复")
    return 0 if ok else 1


# ------------------------------------------------------------------ 入口

def main() -> int:
    p = argparse.ArgumentParser(description="publisher-agent driver")
    p.add_argument("cmd", nargs="?", default="tick",
                   choices=["tick", "run", "gate", "doctor", "status", "report",
                            "plan-set", "deploy", "undeploy", "login", "scan"])
    p.add_argument("arg1", nargs="?", default="default",
                   help="run：流水线名；gate：approve|reject；login：juejin|zhihu|both；plan-set：JSON 文件")
    p.add_argument("arg2", nargs="?", default=None, help="gate：门禁 id（唯一等待中时可省）")
    p.add_argument("--dry", action="store_true", help="干跑：publish 步骤跳过")
    p.add_argument("--json", action="store_true")
    p.add_argument("--plan", default=str(ROOT / "plan.yaml"))
    p.add_argument("--pipeline", default=None, help="流水线 yaml 路径（默认 pipelines/<name>.yaml）")
    args = p.parse_args()

    # 红队：配置/计划损坏不许裸崩——toast + 事件留痕后退出
    NEEDS_PLAN = ("tick", "run", "gate", "doctor", "status", "report")
    try:
        cfg = Config.load()
        plan = Plan(Path(args.plan)) if args.cmd in NEEDS_PLAN else None
    except Exception as e:
        try:
            State((ROOT / "_state")).event("config.load-failed", error=str(e)[:200])
        except Exception:
            pass
        toast("🛑 publisher-agent 配置/计划加载失败", str(e)[:60])
        print(f"✗ 配置加载失败：{e}", file=sys.stderr)
        return 1

    state = State(cfg.state_dir)
    adapter = Adapter(cfg)

    try:
        if args.cmd in ("tick", "run"):
            name = "default" if args.cmd == "tick" else args.arg1
            return run_once(cfg, plan, state, adapter, name, args.dry,
                            Path(args.pipeline) if args.pipeline else None)
        if args.cmd == "gate":
            if args.arg1 not in ("approve", "reject"):
                print("用法：driver gate approve|reject [门禁id]", file=sys.stderr)
                return 1
            gate_id = args.arg2 or _sole_waiting_gate(state)
            if not gate_id:
                print("没有等待中的门禁", file=sys.stderr)
                return 1
            return gate_cmd(cfg, plan, state, args.arg1, gate_id)
        if args.cmd == "doctor":
            return doctor(cfg, plan, state, adapter)
        if args.cmd == "status":
            return status(cfg, plan, state, as_json=args.json)
        if args.cmd == "plan-set":
            if not args.arg1 or args.arg1 == "default":
                print("用法：driver plan-set <JSON 文件路径>", file=sys.stderr)
                return 1
            return plan_set(cfg, Path(args.arg1), Path(args.plan))
        if args.cmd == "deploy":
            return deploy(cfg)
        if args.cmd == "undeploy":
            return undeploy(cfg)
        if args.cmd == "login":
            return login_cmd(cfg, args.arg1 if args.arg1 != "default" else "both")
        if args.cmd == "scan":
            if not args.arg1 or args.arg1 == "default":
                print("用法：driver scan <文件夹或 md 文件路径>", file=sys.stderr)
                return 1
            return scan(cfg, args.arg1)
        return report(cfg, plan, state)
    except Exception:
        log("✗ 异常：\n" + traceback.format_exc())
        state.event("driver.exception", error=traceback.format_exc()[-300:])
        toast("🛑 driver 异常", "详见 events.jsonl / tick.log")
        return 1


def _sole_waiting_gate(state: State) -> str | None:
    ws = state.gates_waiting()
    if len(ws) == 1:
        return next(iter(ws))
    if len(ws) > 1:
        print("多个等待中门禁，请指定 id：" + ", ".join(ws), file=sys.stderr)
    return None


if __name__ == "__main__":
    sys.exit(main())
