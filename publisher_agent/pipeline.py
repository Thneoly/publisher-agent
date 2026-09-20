# -*- coding: utf-8 -*-
"""流水线引擎：pipeline 声明（yaml）→ 按序执行。

- 缺省行为红线：无 pipelines/ 目录或未指定时，内置 default 与旧 tick 逐字等价
- when 谓词闭集：queue_done / picked（红队：dry 是全局语义不是谓词，已删）
- on-fail：continue（默认）/ abort / hold；publish 内部八分支自消化，不走 on-fail
"""
from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import yaml

from .steps import Ctx, REGISTRY, compute_queue_done

ROOT = Path(__file__).resolve().parent.parent

BUILTIN_DEFAULT = {
    "name": "default",
    # v0.4：仅掘金（纯 API，零浏览器）。知乎已剥离——走原 publish.py 通道；
    # 若要恢复双平台，自定义 pipelines/*.yaml 加 zhihu 的 select/publish 步骤即可。
    "steps": [
        {"reconcile": {"daily": True}},
        {"poll": {}},
        {"select": {"platform": "juejin"}},
        {"publish": {"platform": "juejin"}},
        {"disable-tasks": {"when": "queue_done"}},
    ],
}


def load(name: str, path: Path | None = None) -> dict:
    """name=default 且无文件 → 内置；否则读 pipelines/<name>.yaml。"""
    if path is None:
        path = ROOT / "pipelines" / f"{name}.yaml"
    if not path.exists():
        if name == "default":
            return dict(BUILTIN_DEFAULT)
        raise FileNotFoundError(f"流水线不存在：{path}（可 run default 用内置默认）")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    steps = data.get("steps") or []
    for raw in steps:
        stype = next(iter(raw), "")
        if stype not in REGISTRY:
            raise ValueError(f"未知步骤类型 {stype}（可用：{sorted(REGISTRY)}）")
    return {"name": data.get("name", name), "steps": steps}


def run(pipeline: dict, cfg, plan, state, adapter, dry: bool) -> str:
    """执行一条流水线；返回 ended / gate_wait / aborted。"""
    run_id = f"{datetime.now():%m%d%H%M}-{uuid.uuid4().hex[:4]}"
    name = pipeline.get("name", "unnamed")
    ctx = Ctx(cfg, plan, state, adapter, dry, run_id, name)
    state.event("pipeline.start", run_id=run_id, pipeline=name, dry=dry)
    state._data["last_pipeline"] = {"name": name, "run_id": run_id,
                                    "ts": datetime.now().isoformat(timespec="seconds")}
    print(f"── 流水线 {name}（run {run_id}{' · DRY' if dry else ''}）──", flush=True)
    for raw in pipeline.get("steps", []):
        stype, params = next(iter(raw.items()))
        params = dict(params or {})
        when = params.pop("when", None)
        ctx.step = stype
        if when == "queue_done":
            compute_queue_done(ctx)
            if not ctx.queue_done:
                state.event("step.skip", step=stype, run_id=run_id, reason="queue 未完")
                continue
        if when == "picked" and not ctx.picked:
            state.event("step.skip", step=stype, run_id=run_id, reason="无 picked")
            continue
        try:
            res = REGISTRY[stype](ctx, params)
        except Exception as e:                       # 步骤异常按 on-fail 分档
            res = None
            state.event("step.error", step=stype, run_id=run_id, error=str(e)[:200])
            onfail = params.get("on-fail", "continue")
            if onfail == "abort":
                return "aborted"
            continue
        state.event("step.end", step=stype, run_id=run_id,
                    status=res.status, reason=res.reason[:60])
        if res.status == "gate_wait":
            print(f"── 流水线 {name} 在 {stype} 暂停等待人工门禁（正常退出）──", flush=True)
            return "gate_wait"
        if res.status == "fail":
            onfail = params.get("on-fail", "continue")
            if onfail == "abort":
                return "aborted"
    print(f"── 流水线 {name} 完成 ──", flush=True)
    return "ended"
