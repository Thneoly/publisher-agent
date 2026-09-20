# -*- coding: utf-8 -*-
"""计划与配置加载：agent.yaml（站点）+ plan.yaml（用户排期声明）。"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent


def _load_yaml(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"缺少配置文件：{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path} 结构异常：顶层应为映射")
    return data


@dataclass
class Config:
    """agent.yaml 的强类型视图。"""
    engine_script: str
    engine_cwd: Path
    runner: list[str]
    cadence: dict
    state_dir: Path
    tasks: list[str]
    escalate: bool

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        raw = _load_yaml(path or ROOT / "agent.yaml")
        eng = raw.get("engine") or {}
        cwd = Path(eng.get("cwd", "."))
        script = eng.get("script", "publish.py")
        if not (cwd / script).exists() and not Path(script).is_absolute():
            raise FileNotFoundError(f"引擎脚本不存在：{cwd / script}（agent.yaml engine 段）")
        return cls(
            engine_script=str(Path(script) if Path(script).is_absolute() else cwd / script),
            engine_cwd=cwd,
            runner=list(eng.get("runner", ["uv", "run"])),
            cadence=raw.get("cadence") or {},
            state_dir=(ROOT / raw.get("state_dir", "_state")).resolve(),
            tasks=list(raw.get("tasks") or []),
            escalate=bool(raw.get("escalate", False)),
        )


@dataclass
class Item:
    """一条发布计划条目。platforms: {juejin: {at: "2026-09-19 09:15"}, zhihu: {after: juejin, at: …}}"""
    id: str
    file: str
    platforms: dict
    flags: dict = field(default_factory=dict)

    def platform_after(self, platform: str) -> str | None:
        spec = self.platforms.get(platform)
        return spec.get("after") if isinstance(spec, dict) else None

    def platform_at(self, platform: str):
        """平台指定绝对发布时间；无则 None（按队列顺序随时可发）。"""
        from datetime import datetime
        spec = self.platforms.get(platform)
        if not isinstance(spec, dict) or not spec.get("at"):
            return None
        return datetime.strptime(str(spec["at"]), "%Y-%m-%d %H:%M")


class Plan:
    def __init__(self, path: Path):
        raw = _load_yaml(path)
        self.cadence: dict = raw.get("cadence") or {}
        self.items: list[Item] = []
        seen: set[str] = set()
        for entry in raw.get("queue") or []:
            # 平台声明是平铺键（juejin:/zhihu:），与文档 schema 一致
            platforms = {k: (entry[k] if isinstance(entry[k], dict) else {})
                         for k in ("juejin", "zhihu") if k in entry}
            item = Item(id=str(entry["id"]), file=str(entry["file"]),
                        platforms=platforms, flags=entry.get("flags") or {})
            # file 在引擎 cwd 下解析（引擎的 posts 相对路径）
            if item.id in seen:
                raise ValueError(f"计划里 slug 重复：{item.id}")
            seen.add(item.id)
            self.items.append(item)

    def resolve_file(self, item: Item, cfg: Config) -> Path:
        p = Path(item.file)
        return p if p.is_absolute() else cfg.engine_cwd / p

    def post_fingerprint(self, item: Item, cfg: Config) -> dict:
        """发布时刻冻结身份用：解析 frontmatter 标题 + 正文 sha256（只读，不依赖引擎）。"""
        import re
        text = self.resolve_file(item, cfg).read_text(encoding="utf-8")
        tj = re.search(r"^title_juejin:\s*(.+)$", text, re.M)
        tz = re.search(r"^title_zhihu:\s*(.+)$", text, re.M)
        return {
            "title_juejin": tj.group(1).strip().strip('"') if tj else "",
            "title_zhihu": tz.group(1).strip().strip('"') if tz else "",
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()[:8],
        }
