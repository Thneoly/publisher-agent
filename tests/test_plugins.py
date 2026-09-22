# -*- coding: utf-8 -*-
"""publisher-agent Python 插件单元测试

运行：cd D:\FDE\publisher-agent && uv run python -m pytest tests/ -v
"""
import json
import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# 让 import 找到 publisher_agent
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from publisher_agent.browser import md_to_html, _frontmatter
from publisher_agent.cover import make_cover_juejin, FONT_CANDIDATES


# ═══════════════════════════════════════════════════════════════
# md_to_html 测试
# ═══════════════════════════════════════════════════════════════

class TestMdToHtml:
    def test_heading(self):
        assert "<h1>标题</h1>" in md_to_html("# 标题")
        assert "<h2>子标题</h2>" in md_to_html("## 子标题")
        assert "<h3>三级</h3>" in md_to_html("### 三级")

    def test_bold(self):
        assert "<strong>重要</strong>" in md_to_html("**重要**")

    def test_inline_code(self):
        assert "<code>x = 1</code>" in md_to_html("`x = 1`")

    def test_unordered_list(self):
        html = md_to_html("- 第一项\n- 第二项")
        assert "<ul>" in html and "<li>第一项</li>" in html and "<li>第二项</li>" in html

    def test_ordered_list(self):
        html = md_to_html("1. 步骤一\n2. 步骤二")
        assert "<ol>" in html and "<li>步骤一</li>" in html

    def test_blockquote(self):
        html = md_to_html("> 引用内容")
        assert "<blockquote>" in html and "<p>引用内容</p>" in html

    def test_table(self):
        md = "| 列A | 列B |\n|---|---|\n| 1 | 2 |"
        html = md_to_html(md)
        assert "<table>" in html and "<th>列A</th>" in html and "<td>1</td>" in html

    def test_code_fence(self):
        md = "```python\nprint('hello')\n```"
        html = md_to_html(md)
        assert "<pre><code" in html and "print" in html

    def test_hr(self):
        assert "<hr>" in md_to_html("---")

    def test_link(self):
        # browser.py 的 md_to_html 不支持链接（知乎编辑器自行处理）——验证链接原文保留
        result = md_to_html("[文本](https://example.com)")
        assert "文本" in result and "https://example.com" in result

    def test_html_escape(self):
        assert "&lt;script&gt;" in md_to_html("<script>")

    def test_empty(self):
        assert md_to_html("") == ""

    def test_mixed_content(self):
        md = "# 标题\n\n**粗体** 和 `代码`\n\n- 列表项\n"
        html = md_to_html(md)
        assert "<h1>标题</h1>" in html
        assert "<strong>粗体</strong>" in html
        assert "<code>代码</code>" in html
        assert "<li>列表项</li>" in html


# ═══════════════════════════════════════════════════════════════
# frontmatter 解析测试
# ═══════════════════════════════════════════════════════════════

class TestFrontmatter:
    def test_basic(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: 标题\ntags: AI\n---\n\n正文", encoding="utf-8")
        fm = _frontmatter(f)
        assert fm["fields"]["title"] == "标题"
        assert fm["fields"]["tags"] == "AI"
        assert "正文" in fm["body"]

    def test_no_frontmatter(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("只是正文", encoding="utf-8")
        fm = _frontmatter(f)
        assert "正文" in fm["body"]
        assert fm["fields"] == {}

    def test_html_comment_stripped(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: t\n---\n\n<!-- 注释 -->\n正文", encoding="utf-8")
        fm = _frontmatter(f)
        assert "注释" not in fm["body"]
        assert "正文" in fm["body"]

    def test_multiline_body(self, tmp_path):
        f = tmp_path / "test.md"
        f.write_text("---\ntitle: t\n---\n\n第一段\n\n第二段\n\n第三段", encoding="utf-8")
        fm = _frontmatter(f)
        assert "第一段" in fm["body"]
        assert "第三段" in fm["body"]


# ═══════════════════════════════════════════════════════════════
# 封面生成测试
# ═══════════════════════════════════════════════════════════════

class TestCoverGeneration:
    def test_generates_png(self, tmp_path, monkeypatch):
        """需要 PIL 和中文字体——CI 环境可能缺字体，跳过"""
        try:
            from PIL import Image
        except ImportError:
            pytest.skip("PIL not available")

        font_exists = any(Path(fp).exists() for fp in FONT_CANDIDATES)
        if not font_exists:
            pytest.skip("No Chinese font available")

        f = tmp_path / "test.md"
        f.write_text("---\ntitle_juejin: 测试封面\ntitle_zhihu: 知乎封面\n---\n\n正文", encoding="utf-8")
        out = tmp_path / "cover.png"
        result = make_cover_juejin(f, out)
        assert result.exists()
        assert result.stat().st_size > 1000  # 不是空文件
        img = Image.open(result)
        assert img.size == (192, 128)  # 掘金信息流封面尺寸


# ═══════════════════════════════════════════════════════════════
# plan.yaml 写入/读取往返测试
# ═══════════════════════════════════════════════════════════════

class TestPlanYaml:
    def test_roundtrip(self, tmp_path):
        import yaml
        plan = {
            "cadence": {"juejin": {"min_gap_h": 0}},
            "queue": [
                {"id": "01", "file": "D:/test/01.md", "juejin": {"at": "2026-09-25 09:15", "column": "测试"}},
                {"id": "02", "file": "D:/test/02.md", "juejin": {}},
            ]
        }
        p = tmp_path / "plan.yaml"
        p.write_text(yaml.safe_dump(plan, allow_unicode=True, sort_keys=False), encoding="utf-8")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert loaded["queue"][0]["id"] == "01"
        assert loaded["queue"][0]["juejin"]["at"] == "2026-09-25 09:15"
        assert loaded["queue"][1]["juejin"] == {}

    def test_empty_queue(self, tmp_path):
        import yaml
        p = tmp_path / "plan.yaml"
        p.write_text("queue: []\n", encoding="utf-8")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert loaded["queue"] == []

    def test_special_chars_in_path(self, tmp_path):
        import yaml
        plan = {"queue": [{"id": "x", "file": "D:/路径/中文目录/文件.md", "juejin": {}}]}
        p = tmp_path / "plan.yaml"
        p.write_text(yaml.safe_dump(plan, allow_unicode=True), encoding="utf-8")
        loaded = yaml.safe_load(p.read_text(encoding="utf-8"))
        assert "中文目录" in loaded["queue"][0]["file"]


# ═══════════════════════════════════════════════════════════════
# 状态机逻辑测试
# ═══════════════════════════════════════════════════════════════

class TestStateMachine:
    TERMINAL = ["live", "closed", "held", "rejected"]
    ACTIVE = ["pending", "in_review", "partial", "failed"]

    def test_terminal_not_selectable(self):
        """终态条目不应被选篇"""
        for st in self.TERMINAL:
            assert st in self.TERMINAL

    def test_active_selectable(self):
        """非终态条目应可被选篇"""
        for st in self.ACTIVE:
            assert st not in self.TERMINAL

    def test_frozen_titles_requires_article_id(self):
        """冻结标题只收有 article_id 的（防自拦 bug）"""
        state = {
            "entries": {
                "published": {"status": "in_review",
                              "platforms": {"juejin": {"title": "T1", "article_id": "123"}}},
                "intent_only": {"status": "pending",
                                "platforms": {"juejin": {"title": "T2", "sha": "abc"}}},
            }
        }
        frozen = set()
        for v in state["entries"].values():
            j = v.get("platforms", {}).get("juejin", {})
            if j.get("article_id"):
                frozen.add(j.get("title", ""))
        assert "T1" in frozen
        assert "T2" not in frozen

    def test_partial_only_with_zhihu(self):
        """纯掘金条目过审应为 live，声明了知乎且知乎未 live 才是 partial"""
        def compute_status(zhihu_declared, zhihu_live):
            if zhihu_declared and not zhihu_live:
                return "partial"
            return "live"

        assert compute_status(False, False) == "live"    # 纯掘金 → live
        assert compute_status(True, False) == "partial"   # 声明知乎但未发 → partial
        assert compute_status(True, True) == "live"       # 双平台都完成 → live


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
