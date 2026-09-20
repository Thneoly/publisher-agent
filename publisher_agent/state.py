# -*- coding: utf-8 -*-
"""状态与审计：pub_state.json（原子回写、身份冻结）+ events.jsonl（append-only）+ lockfile + 心跳。"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

LOCK_STALE_MIN = 30


class State:
    def __init__(self, state_dir: Path):
        self.dir = state_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = state_dir / "pub_state.json"
        self.events = state_dir / "events.jsonl"
        self.lock = state_dir / "tick.lock"
        self.heartbeat = state_dir / "heartbeat"
        self._data = self._load()

    # ------------------------------------------------------------------ state
    def _load(self) -> dict:
        if self.path.exists():
            try:
                return json.loads(self.path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # 损坏即备份后重建（state 可由对账+平台事实重建）
                self.path.rename(self.path.with_suffix(".corrupt"))
        return {"entries": {}}

    def save(self) -> None:
        """tmp+rename 原子替换——计划任务并发/崩溃时不写半个文件。"""
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)

    def entry(self, slug: str) -> dict:
        return self._data["entries"].setdefault(slug, {"status": "pending", "platforms": {}})

    def set_platform(self, slug: str, platform: str, **kv) -> None:
        e = self.entry(slug)
        e["platforms"].setdefault(platform, {})
        e["platforms"][platform].update(kv)

    def plat(self, slug: str, platform: str) -> dict:
        return self.entry(slug).get("platforms", {}).get(platform, {})

    def status(self, slug: str) -> str:
        return self.entry(slug).get("status", "pending")

    def set_status(self, slug: str, status: str) -> None:
        self.entry(slug)["status"] = status
        self.entry(slug)["updated"] = datetime.now().isoformat(timespec="seconds")

    # ------------------------------------------------------------- 事实查询
    def published_today(self, platform: str) -> int:
        today = datetime.now().date().isoformat()
        n = 0
        for e in self._data["entries"].values():
            p = (e.get("platforms") or {}).get(platform) or {}
            if str(p.get("published_at", ""))[:10] == today:
                n += 1
        return n

    def last_publish_ts(self, platform: str) -> float:
        ts = [p.get("published_ts", 0)
              for e in self._data["entries"].values()
              for k, p in (e.get("platforms") or {}).items() if k == platform]
        return max(ts) if ts else 0.0

    def frozen_titles(self, platform: str) -> set[str]:
        """发布时冻结的标题表——磁盘文件后续改名/改标题都不会让已发条目被重复发布。"""
        out = set()
        for e in self._data["entries"].values():
            t = (e.get("platforms") or {}).get(platform, {}).get("title")
            if t:
                out.add(t)
        return out

    # ---------------------------------------------------------------- events
    def event(self, type_: str, **kv) -> None:
        rec = {"ts": datetime.now().isoformat(timespec="seconds"), "type": type_, **kv}
        with open(self.events, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    # ----------------------------------------------------------------- gates
    def gate(self, gate_id: str) -> dict:
        return self._data.setdefault("gates", {}).setdefault(gate_id, {})

    def gates_waiting(self) -> dict:
        return {k: v for k, v in self._data.get("gates", {}).items()
                if v.get("status") == "waiting"}

    def slug_gated(self, slug: str) -> str | None:
        """slug 被任一 waiting gate 挡住 → 返回 gate_id（红队 blocker：gate 是 state 层事实，
        对所有流水线生效，default 也绕不过）。"""
        for gid, g in self.gates_waiting().items():
            if g.get("slug") == slug:
                return gid
        return None

    def set_gate(self, gate_id: str, **kv) -> None:
        self.gate(gate_id).update(kv)
        self.save()

    def touch_heartbeat(self) -> None:
        self.heartbeat.write_text(datetime.now().isoformat(timespec="seconds"), encoding="utf-8")

    # ------------------------------------------------------------------ lock
    def acquire(self) -> bool:
        """lockfile 带 PID+时间戳；超 30 分钟判陈旧破锁并告警（红队：崩溃锁永久噤声）。"""
        if self.lock.exists():
            age_min = (time.time() - self.lock.stat().st_mtime) / 60
            if age_min > LOCK_STALE_MIN:
                self.event("lock.stale-broken", age_min=round(age_min, 1))
                self.lock.unlink()
            else:
                return False
        self.lock.write_text(str(os.getpid()), encoding="utf-8")
        return True

    def release(self) -> None:
        self.lock.unlink(missing_ok=True)
