# -*- coding: utf-8 -*-
"""只读哨兵（红队 blocker 修法）：

- juejin_recent(cookie): 引擎探针拉已发列表（标题+ctime）——对账与发布前去重
- zhihu_article_alive(url, expect_title): CDP 打开 /p/<id> 核标题存在——
  知乎发布「结果未知」后的核验、重试前预检
- 路由漂移检测：页签标题异常即报 ROUTE_DRIFT（知乎改版哨兵）

全部只读，不点击任何按钮。
"""
from __future__ import annotations

import json
import time


def juejin_recent(adapter) -> list[dict]:
    """[{title, article_id, ctime}]，最新在前。失败抛异常由 driver 记 incident。"""
    code = (
        "(lambda d: [(((x.get('article_info') or {}).get('title') or ''), "
        "str((x.get('article_info') or {}).get('article_id') or ''), "
        "float((x.get('article_info') or {}).get('ctime') or 0)) "
        "for x in (d if isinstance(d, list) else ((d or {}).get('data') or []))])"
        "(P.api('/content_api/v1/article/list_by_user', "
        "{'user_id': P.load_meta().get('user_id'), 'cursor': '0', 'limit': 30, 'sort_type': 2}, "
        "P.load_cookie()).get('data'))"
    )
    data = adapter.probe_json(code)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"掘金对账探针失败：{data['error']}")
    return [{"title": t, "article_id": i, "ctime": c} for t, i, c in data if t]


class ZhihuProbe:
    """极简 CDP 客户端（只读导航+取 DOM），不依赖引擎代码。"""

    def __init__(self):
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:9222/json/new?about:blank", method="PUT")
        with urllib.request.urlopen(req, timeout=5) as r:
            target = json.loads(r.read().decode("utf-8"))
        from websockets.sync.client import connect as ws_connect
        self._cm = ws_connect(target["webSocketDebuggerUrl"], open_timeout=10, close_timeout=5)
        self.ws = self._cm.__enter__()
        self._id = 0
        self.target_id = target.get("id")

    def close(self):
        try:
            self.call("Target.closeTarget", targetId=self.target_id)
        except Exception:
            pass
        self._cm.__exit__(None, None, None)

    def call(self, method: str, **params):
        self._id += 1
        self.ws.send(json.dumps({"id": self._id, "method": method, "params": params}))
        deadline = time.time() + 30
        while time.time() < deadline:
            msg = json.loads(self.ws.recv(timeout=max(1, deadline - time.time())))
            if msg.get("id") == self._id:
                if "error" in msg:
                    raise RuntimeError(f"CDP {method}: {msg['error']}")
                return msg.get("result", {})
        raise TimeoutError(f"CDP {method} 超时")

    def evaluate(self, js: str):
        r = self.call("Runtime.evaluate", expression=js, returnByValue=True)
        if r.get("exceptionDetails"):
            raise RuntimeError(r["exceptionDetails"].get("text", "JS 异常"))
        return r.get("result", {}).get("value")


def zhihu_article_alive(url: str, expect_title: str | None = None) -> str:
    """alive / title-mismatch:… / dead / drift。优先内嵌 Chromium（Playwright），
    未安装时回退宿主 CDP 探针。删文/404/被重定向走 → dead。"""
    try:
        from . import browser          # Playwright 引擎在则优先（不依赖宿主 9222）
        return browser.zhihu_article_alive(url, expect_title)
    except ImportError:
        pass
    p = ZhihuProbe()
    try:
        p.call("Page.navigate", url=url)
        for _ in range(20):
            time.sleep(1)
            state = p.evaluate("""(() => {
              const t = document.querySelector('h1');
              const h1 = t ? t.innerText.trim() : '';
              const body = document.body.innerText || '';
              if (body.includes('你似乎来到了没有知识存在的荒原') || body.includes('404')
                  || h1 === '请求错误' || body.includes('无法访问当前页面')) return '404';
              if (!location.href.includes('/p/')) return 'redirected';
              return h1.slice(0, 60);
            })()""")
            if state:
                if state in ("404", "redirected"):
                    return "dead"
                if expect_title and expect_title[:10] not in state:
                    return f"title-mismatch:{state}"
                return "alive"
        return "drift"
    finally:
        p.close()
