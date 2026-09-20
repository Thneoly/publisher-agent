# -*- coding: utf-8 -*-
"""内嵌浏览器引擎（Playwright + 自带 Chromium）——不依赖宿主 Chrome/9222。

- 持久化 profile：_state/chromium-profile（登录态由应用自己持有）
- 知乎发布全链路（fill+cover+一键直发）与只读核验从原 CDP 版移植
- 掘金扫码登录（Cookie 喂给引擎 publish.py 的 .juejin.env）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .plan import ROOT

PROFILE_DIR = ROOT / "_state" / "chromium-profile"

_ctx = None
_pw = None


def context(headless: bool = False):
    """共享的持久化浏览器上下文（懒加载；知乎反 headless，默认有头）。"""
    global _ctx, _pw
    if _ctx is None:
        from playwright.sync_api import sync_playwright
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        try:
            _pw = sync_playwright().start()
            _ctx = _pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), headless=headless,
                args=["--disable-blink-features=AutomationControlled"],
                viewport={"width": 1280, "height": 900})
        except Exception as e:
            # 最常见：profile 被上一个未退干净的实例占用（Chrome 报中文错并秒退）
            if _pw is not None:
                try:
                    _pw.stop()
                except Exception:
                    pass
                _pw = None
            msg = str(e).splitlines()[0][:160]
            raise RuntimeError(
                f"内嵌 Chromium 启动失败（{msg}）——多为上一次实例仍占着 profile："
                f"到任务管理器结束 ms-playwright\\chromium 的 chrome.exe 后重试") from e
    return _ctx


def shutdown() -> None:
    """收尾：关上下文+停 playwright。不调用会让 Python 进程挂在退出阶段（sync 线程不退出）。"""
    global _ctx, _pw
    if _ctx is not None:
        try:
            _ctx.close()
        except Exception:
            pass
        _ctx = None
    if _pw is not None:
        try:
            _pw.stop()
        except Exception:
            pass
        _pw = None


def page(url: str = "about:blank", headless: bool = False):
    ctx = context(headless)
    p = ctx.pages[0] if ctx.pages and url == "about:blank" else ctx.new_page()
    if url != "about:blank":
        p.goto(url, wait_until="domcontentloaded", timeout=60000)
    return p


# -------------------------------------------------------------------- 登录

def login_juejin(engine_env_file: Path, timeout_min: int = 5) -> bool:
    """掘金扫码登录：抓 sessionid 写引擎 .juejin.env（只写这一件事，别的不碰）。"""
    p = page("https://juejin.cn")
    try:
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            cookies = p.context.cookies(["https://juejin.cn"])
            sid = next((c for c in cookies if c["name"] == "sessionid"), None)
            if sid:
                header = "; ".join(f"{c['name']}={c['value']}" for c in cookies
                                   if c.get("name") and c.get("value"))
                engine_env_file.write_text(f"JUEJIN_COOKIE={header}\n", encoding="utf-8")
                return True
            p.wait_for_timeout(3000)
        return False
    finally:
        p.close()


def login_zhihu(timeout_min: int = 5) -> bool:
    """知乎扫码登录：登录态存持久化 profile。"""
    p = page("https://www.zhihu.com/signin?next=%2Fcreator")
    try:
        deadline = time.time() + timeout_min * 60
        while time.time() < deadline:
            if p.context.cookies(["https://www.zhihu.com"]) and p.url.find("signin") < 0:
                return True
            if any(c["name"] in ("z_c_y",) for c in p.context.cookies(["https://www.zhihu.com"])):
                return True
            p.wait_for_timeout(3000)
        return False
    finally:
        p.close()


def zhihu_logged_in() -> bool:
    return any(c["name"] == "z_c_y"
               for c in context().cookies(["https://www.zhihu.com"]))


# -------------------------------------------------------------------- 发布

def md_to_html(md: str) -> str:
    """Markdown→知乎可粘贴 HTML（与引擎 publish.py 同口径的轻量版：标题/加粗/行内码/
    列表/引用/表格/代码块/链接/分隔线）。"""
    import re

    def esc(s): return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    def inline(s):
        s = esc(s)
        s = re.sub(r"`([^`]+)`", r"<code>\1</code>", s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", s)
        return s

    out, lines, i = [], md.splitlines(), 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1; continue
        if s.startswith("```"):
            lang, code = s[3:].strip(), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i]); i += 1
            i += 1
            out.append(f'<pre><code data-lang="{esc(lang)}">{esc(chr(10).join(code))}</code></pre>')
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            out.append(f"<h{len(m.group(1))}>{inline(m.group(2))}</h{len(m.group(1))}>"); i += 1; continue
        if re.match(r"^(-{3,}|\*{3,})$", s):
            out.append("<hr>"); i += 1; continue
        if s.startswith(">"):
            q = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                q.append(re.sub(r"^\s*>\s?", "", lines[i].strip())); i += 1
            inner = "".join(f"<p>{inline(x)}</p>" for x in q)
            out.append(f"<blockquote>{inner}</blockquote>"); continue
        if s.startswith("|") and i + 1 < len(lines) and re.match(r"^\|[\s:|-]+\|?$", lines[i + 1].strip()):
            rows = [s]; i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(lines[i].strip()); i += 1
            cells = [[c.strip() for c in r.strip("|").split("|")] for r in rows]
            th = "".join(f"<th>{inline(c)}</th>" for c in cells[0])
            trs = "".join("<tr>" + "".join(f"<td>{inline(c)}</td>" for c in r) + "</tr>" for r in cells[1:])
            out.append(f"<table><thead><tr>{th}</tr></thead><tbody>{trs}</tbody></table>"); continue
        if re.match(r"^[-*]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^[-*]\s+", lines[i].strip()):
                items.append(f"<li>{inline(re.sub(r'^[-*]\s+', '', lines[i].strip()))}</li>"); i += 1
            out.append(f"<ul>{''.join(items)}</ul>"); continue
        if re.match(r"^\d+[.、]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\d+[.、]\s+", lines[i].strip()):
                txt = re.sub(r"^\d+[.、]\s+", "", lines[i].strip())
                items.append(f"<li>{inline(txt)}</li>"); i += 1
            out.append(f"<ol>{''.join(items)}</ol>"); continue
        out.append(f"<p>{inline(s)}</p>"); i += 1
    return "\n".join(out)


def _frontmatter(path: Path) -> dict:
    import re
    text = path.read_text(encoding="utf-8")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, flags=re.S)
    fields, body = {}, m.group(2) if m else text
    if m:
        for line in m.group(1).splitlines():
            km = re.match(r"^([A-Za-z_]+)\s*:\s*(.*)$", line.strip())
            if km:
                fields[km.group(1)] = km.group(2).strip().strip('"').strip("'")
    clean = re.sub(r"<!--.*?-->", "", body, flags=re.S)
    return {"fields": fields, "body": re.sub(r"\n{3,}", "\n\n", clean).strip() + "\n"}


def zhihu_publish(md_path: Path, cover_png: Path | None) -> tuple[str, str | None]:
    """知乎发布（Playwright 版）。返回 (result, url)：
    result ∈ published / no-login / no-button / unknown / drift"""
    if not zhihu_logged_in():
        return "no-login", None
    fm = _frontmatter(md_path)
    title = fm["fields"].get("title_zhihu", "")
    html = md_to_html(fm["body"])
    p = page("https://zhuanlan.zhihu.com/write")
    try:
        p.wait_for_selector("textarea", timeout=30000)
        # 清旧防重贴 + 填标题 + 注正文（与 CDP 版同语义）
        p.evaluate("""(title) => {
          const ta = document.querySelector('textarea');
          const setter = Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set;
          setter.call(ta, title);
          ta.dispatchEvent(new Event('input', {bubbles: true}));
        }""", title)
        n = p.evaluate("""(html) => {
          const ed = document.querySelector('[contenteditable="true"]');
          ed.focus();
          const s0 = window.getSelection();
          if ((ed.innerText || '').trim().length > 0) { s0.selectAllChildren(ed); document.execCommand('delete'); }
          const dt = new DataTransfer();
          dt.setData('text/html', html);
          dt.setData('text/plain', html);
          ed.dispatchEvent(new ClipboardEvent('paste', {clipboardData: dt, bubbles: true, cancelable: true}));
          return ed.innerText.length;
        }""", html)
        if not n:
            return "drift", None
        # 封面（失败不致命）
        if cover_png and Path(cover_png).exists():
            try:
                inp = p.locator("input.UploadPicture-input").first
                inp.scroll_into_view_if_needed(timeout=5000)
                inp.set_input_files(str(cover_png))
                p.wait_for_timeout(8000)
            except Exception:
                pass
        # 一键直发（知乎记住创作声明设置）
        btn = p.locator("button", has_text="发布").last
        if not btn.count():
            return "no-button", None
        btn.click()
        for _ in range(25):
            p.wait_for_timeout(1000)
            url = p.url
            if "/p/" in url and "/edit" not in url:
                return "published", url
            if "发布成功" in (p.inner_text("body") or ""):
                p.wait_for_timeout(2000)
                return "published", p.url
        return "unknown", None
    finally:
        # 分享弹窗尽力关掉，留干净 profile
        try:
            p.keyboard.press("Escape")
        except Exception:
            pass
        p.close()


def make_cover_zhihu(md_path: Path, out_png: Path) -> Path:
    """知乎封面 1200×675（与引擎同款深底设计，PIL 生成）。"""
    import re
    from PIL import Image, ImageDraw, ImageFont
    fm = _frontmatter(md_path)
    w, h = 1200, 675
    img = Image.new("RGB", (w, h), "#121217")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 18], fill="#2e6cff")
    font = mid = small = None
    for fp in (r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf", r"C:\Windows\Fonts\simhei.ttf"):
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 88)
                mid = ImageFont.truetype(fp, 56)
                small = ImageFont.truetype(fp, 40)
                break
            except Exception:
                continue
    if font is None:
        raise RuntimeError("找不到中文字体")
    brand = re.sub(r"[《》\s]", "", fm["fields"].get("title_zhihu", ""))[:8] or "BLOG"
    d.text((72, 150), brand, font=font, fill="#ffffff")
    sub = re.sub(r"[《》]", "", fm["fields"].get("title_zhihu", ""))[:22]
    d.text((72, 420), sub, font=mid, fill="#c8cdd8")
    d.text((72, 560), str(fm["fields"].get("description", ""))[:24], font=small, fill="#6b7280")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


# -------------------------------------------------------------------- 只读核验

def zhihu_article_alive(url: str, expect_title: str | None) -> str:
    """alive / title-mismatch:… / dead / drift（Playwright 版，替代原 ZhihuProbe）。"""
    if not zhihu_logged_in():
        return "drift"
    p = page(url)
    try:
        p.wait_for_load_state("domcontentloaded", timeout=30000)
        for _ in range(15):
            body = p.inner_text("body") or ""
            h1 = (p.query_selector("h1").inner_text().strip()
                  if p.query_selector("h1") else "")
            if "你似乎来到了没有知识存在的荒原" in body or "404" in body \
                    or h1 == "请求错误" or "无法访问当前页面" in body:
                return "dead"
            if "/p/" not in p.url:
                return "dead"
            if h1:
                if expect_title and expect_title[:10] not in h1:
                    return f"title-mismatch:{h1[:40]}"
                return "alive"
            p.wait_for_timeout(1000)
        return "drift"
    finally:
        p.close()
