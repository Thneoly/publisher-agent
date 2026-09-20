# -*- coding: utf-8 -*-
"""引擎适配层（子进程契约）：调用原 juejin-publisher 的 publish.py，一行不改。

错误分类靠退出码 + 已知消息模式（零侵入的代价——模式表随引擎版本 pin，
契约基线：juejin-publisher v1.0.6）。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
from dataclasses import dataclass

# 消息模式表：按序首个命中即分类（None 之外的都算终态判定）
PATTERNS: list[tuple[str, str]] = [
    ("COOKIE_EXPIRED", r"HTTP 40[13]|Cookie 失效|未配置 Cookie|401"),
    ("ROUTE_DRIFT",    r"未找到.*发布按钮|超时——浏览器|结构可能变了|未检测到知乎登录超时"),
    ("PLATFORM_ERROR", r"建草稿失败|发布失败|返回异常|网络错误|一个标签都没解析到|审核未通过"),
    ("AUDIT_REJECTED", r"已被驳回"),
]

RE_JUEJIN_ID = re.compile(r"juejin\.cn/post/(\d+)")
RE_ZHIHU_URL = re.compile(r"知乎已自动发布[：:]\s*(https://\S+)")


@dataclass
class EngineResult:
    ok: bool
    kind: str            # OK / COOKIE_EXPIRED / ROUTE_DRIFT / PLATFORM_ERROR / AUDIT_REJECTED / UNKNOWN_RESULT
    output: str
    juejin_id: str = ""
    zhihu_url: str = ""


def _classify(output: str) -> str:
    for kind, pat in PATTERNS:
        if re.search(pat, output):
            return kind
    return "UNKNOWN_RESULT" if output.strip() else "UNKNOWN_RESULT"


def _run(cfg, args: list[str], timeout: int = 900) -> EngineResult:
    cmd = [*cfg.runner, cfg.engine_script, *args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, cwd=str(cfg.engine_cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired as e:
        return EngineResult(False, "ROUTE_DRIFT", f"引擎超时 {timeout}s：{e}")
    ok = r.returncode == 0
    kind = "OK" if ok else _classify(out)
    # 成功输出里也可能带失败语义（zhihu 结果未知：无 URL、无硬错）
    if ok:
        m = RE_ZHIHU_URL.search(out) or RE_JUEJIN_ID.search(out)
        if not m and any(a == "zhihu" for a in args):
            kind = "UNKNOWN_RESULT"      # 点击已派发但没等到跳转——held，先核验（红队 blocker）
            ok = False
    jid = RE_JUEJIN_ID.search(out)
    zurl = RE_ZHIHU_URL.search(out)
    return EngineResult(ok=ok, kind=kind, output=out,
                        juejin_id=jid.group(1) if jid else "",
                        zhihu_url=zurl.group(1) if zurl else "")


class Adapter:
    """对引擎 publish.py 的全部调用走这里。"""

    def __init__(self, cfg):
        self.cfg = cfg

    def publish_juejin(self, file_rel: str, column: str | None = None) -> EngineResult:
        """发布掘金；column 给定时先切引擎默认专栏（文件夹名→专栏自动匹配，
        零侵入实现：columns --use <id> 后 publish，引擎 meta 记住专栏）。"""
        if column:
            cid = self._column_id(column)
            if cid:
                _run(self.cfg, ["columns", "--use", cid], timeout=60)
            else:
                print(f"⚠ 专栏「{column}」在掘金未找到——按当前默认专栏发布")
        return _run(self.cfg, ["publish", file_rel])

    def _column_id(self, name: str) -> str | None:
        """专栏名→id（去空白后模糊匹配 column_version.title）。"""
        import re as _re
        norm = lambda s: _re.sub(r"\s+", "", str(s or ""))
        target = norm(name)
        cols = self.probe_json(
            "(lambda d: [((x.get('column') or {}).get('column_id'), "
            "((x.get('column_version') or {}).get('title') or '')) "
            "for x in (d if isinstance(d, list) else ((d or {}).get('data') or []))])"
            "(P.api('/content_api/v1/column/self_center_list', "
            "{'user_id': P.load_meta().get('user_id'), 'cursor': '0', 'keyword': '', 'limit': 20}, "
            "P.load_cookie()).get('data'))")
        for cid, title in cols:
            if norm(title) == target or target in norm(title) or norm(title) in target:
                return str(cid)
        return None

    def publish_zhihu(self, file_rel: str) -> EngineResult:
        """知乎已从 agent 剥离（v0.4 用户决策）——保留引擎透传作为兼容备用：
        调原 publish.py zhihu --auto（宿主 CDP 通道）。默认流水线不会走到这里。"""
        return _run(self.cfg, ["zhihu", file_rel, "--auto"])

    def juejin_status(self, article_id: str) -> str:
        """返回 live / rejected / reviewing / unknown（解析 status 命令输出，audit_status 优先口径）。"""
        r = _run(self.cfg, ["status", article_id], timeout=120)
        out = r.output
        if "已被驳回" in out:
            return "rejected"
        if "已上线" in out:
            return "live"
        if "审核中" in out:
            return "reviewing"
        return "unknown"

    def probe_json(self, code: str, timeout: int = 120) -> dict:
        """在引擎 cwd import 其 publish 模块执行只读探针，回 JSON（对账/列表用，不外发）。
        输出里混有 uv 的 venv 警告行——只解析首个合法 JSON 行。"""
        wrapped = (f"import sys, json; sys.path.insert(0, '.'); sys.stdout.reconfigure(encoding='utf-8'); "
                   f"import publish as P; cookie = P.load_cookie(); print(json.dumps({code}))")
        r = _run_raw(self.cfg, ["-c", wrapped], timeout=timeout)
        for line in r.splitlines():
            line = line.strip()
            if line[:1] in ("[", "{"):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        return {"error": r[-300:]}

    def whoami(self) -> EngineResult:
        """引擎 whoami：真实校验掘金 Cookie 有效性（返回账号名或失效提示）。"""
        return _run(self.cfg, ["whoami"], timeout=60)

    def verify_cdp_alive(self) -> bool:
        import urllib.request
        try:
            with urllib.request.urlopen("http://127.0.0.1:9222/json/version", timeout=2) as r:
                return r.status == 200
        except Exception:
            return False


def _run_raw(cfg, py_args: list[str], timeout: int = 120) -> str:
    """跑 python -c（runner 是 uv run 时等价于 uv run python -c）。"""
    cmd = [*cfg.runner, "python", *py_args]
    env = dict(os.environ, PYTHONIOENCODING="utf-8")
    try:
        r = subprocess.run(cmd, cwd=str(cfg.engine_cwd), capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, env=env)
        return (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return "TIMEOUT"
