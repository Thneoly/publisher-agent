# -*- coding: utf-8 -*-
"""封面插件：PIL 生成 192×128 → 内嵌 Chromium 上传进掘金草稿 → 打印封面 URL。

用法：python -m publisher_agent.cover <draft_id> <md 文件>
Rust 发布流在建稿后调用；失败打印 SKIP，发布继续（优雅降级）。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from .browser import _frontmatter, context

FONT_CANDIDATES = [
    r"C:\Windows\Fonts\msyh.ttc", r"C:\Windows\Fonts\msyh.ttf",
    r"C:\Windows\Fonts\simhei.ttf", r"C:\Windows\Fonts\simsun.ttc",
]


def make_cover_juejin(md_path: Path, out_png: Path) -> Path:
    """192×128 信息流封面：深底 + 蓝条 + 标题截断（与引擎 PIL 同款设计）。"""
    from PIL import Image, ImageDraw, ImageFont
    fm = _frontmatter(md_path)
    w, h = 192, 128
    img = Image.new("RGB", (w, h), "#121217")
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 6], fill="#2e6cff")
    font = small = None
    for fp in FONT_CANDIDATES:
        if Path(fp).exists():
            try:
                font = ImageFont.truetype(fp, 26)
                small = ImageFont.truetype(fp, 14)
                break
            except Exception:
                continue
    if font is None:
        raise RuntimeError("找不到中文字体")
    brand = re.sub(r"[《》\s]", "", fm["fields"].get("title_juejin", ""))[:6] or "JUEJIN"
    d.text((12, 26), brand, font=font, fill="#ffffff")
    sub = re.sub(r"[《》]", "", fm["fields"].get("title_zhihu", ""))[:14]
    d.text((12, 84), sub, font=small, fill="#9aa0b0")
    out_png.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_png)
    return out_png


def upload_into_draft(draft_id: str, md_path: Path) -> str | None:
    """打开草稿编辑器 → 点「发布」弹出对话框（封面选择器在里面）→ 上传 → 返回预览 URL。
    只传封面不点确认——TOS 上传完成后编辑器会把封面存进草稿。"""
    cover_png = Path(__import__("os").environ.get(
        "PUBLISHER_AGENT_ROOT", str(Path(__file__).resolve().parent.parent))) \
        / "_state" / "covers" / f"{md_path.stem}.png"
    make_cover_juejin(md_path, cover_png)
    ctx = context(headless=False)
    p = ctx.new_page()
    try:
        p.goto(f"https://juejin.cn/editor/drafts/{draft_id}",
               wait_until="domcontentloaded", timeout=60000)
        p.wait_for_timeout(6000)          # 编辑器是重 SPA：给足渲染时间，不赌选择器时序
        # 封面选择器在发布弹窗内：点「发布」按钮把弹窗带出来（与引擎 upload_cover 同法）
        clicked = False
        for _ in range(3):
            try:
                btn = p.get_by_role("button", name="发布", exact=True).last
                btn.click(timeout=8000)
                clicked = True
                break
            except Exception:
                p.wait_for_timeout(3000)
        if not clicked:
            _dump(p, "no-pub-btn")
            return None
        try:
            p.wait_for_selector(".coverselector_container", state="attached", timeout=20000)
        except Exception:
            _dump(p, "no-coverselector")
            return None
        p.wait_for_timeout(1500)
        inp = p.locator(".coverselector_container input[type=file]").first
        inp.set_input_files(str(cover_png))          # 隐藏 input 无需可见即可设文件
        # 等预览出现（TOS 上传），再给编辑器的草稿自动保存留足时间——
        # 提前返回会让紧随其后的 publish 抢在草稿保存前发出去（实测丢封面）
        for _ in range(20):
            p.wait_for_timeout(1000)
            url = p.evaluate("""(() => {
              const img = document.querySelector('.coverselector_container .preview-box img');
              return img ? img.src.split('?')[0] : '';
            })()""")
            if url:
                p.wait_for_timeout(8000)      # 草稿保存防抖+落盘
                return url
        _dump(p, "no-preview")
        return None
    finally:
        try:
            p.close()
        except Exception:
            pass


def _dump(p, tag: str) -> None:
    """失败留证：截图 + 可见弹窗文本，便于排障。"""
    try:
        root = Path(__file__).resolve().parent.parent / "_state" / "covers"
        root.mkdir(parents=True, exist_ok=True)
        p.screenshot(path=str(root / f"debug_{tag}.png"))
        vis = p.evaluate("""(() =>
          [...document.querySelectorAll('[class*=Modal],[class*=modal],[class*=dialog]')]
            .filter(e => e.offsetParent !== null && (e.innerText||'').trim())
            .map(e => (e.innerText||'').slice(0,80)).slice(0,3))()""")
        print(f"DEBUG[{tag}] 可见弹窗: {vis}")
    except Exception:
        pass


def main() -> int:
    if len(sys.argv) < 3:
        print("SKIP 用法：cover <draft_id> <md>")
        return 1
    draft_id, md = sys.argv[1], Path(sys.argv[2])
    try:
        url = upload_into_draft(draft_id, md)
        if url:
            print(f"COVER_URL {url}")
            return 0
        print("SKIP 封面上传后未见预览")
        return 1
    except Exception as e:
        print(f"SKIP {str(e)[:120]}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
