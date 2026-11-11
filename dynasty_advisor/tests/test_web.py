"""The page renders untrusted names inside the authenticated HA frontend."""

from __future__ import annotations

import unittest

import _fixtures  # noqa: F401
from advisor.web import REPORT_RE, md_to_html, report_label


class TestRender(unittest.TestCase):
    def test_everything_is_escaped(self):
        md = "### 1. <script>alert(1)</script> — seller\n- **Open with:** Give **<img src=x onerror=y>**\n| a | b |\n|---|---|\n| <b>x</b> | 1 |\n"
        out = md_to_html(md)
        self.assertNotIn("<script>", out)
        self.assertNotIn("<img", out)
        self.assertNotIn("<b>x", out)
        self.assertIn("&lt;script&gt;", out)
        self.assertIn("<strong>&lt;img src=x onerror=y&gt;</strong>", out)

    def test_structure(self):
        out = md_to_html("## Trades\n> **Deadline**\n\n| A | B |\n|---|---|\n| 1 | 2 |\n\nplain line\n---\n")
        for tag in ("<h2>Trades</h2>", "<blockquote><strong>Deadline</strong></blockquote>",
                    "<th>A</th>", "<td>2</td>", "<p>plain line</p>", "<hr>"):
            self.assertIn(tag, out)

    def test_report_names_are_whitelisted(self):
        self.assertTrue(REPORT_RE.match("2026-11-11-124916.md"))
        self.assertTrue(REPORT_RE.match("2026-11-11.md"))
        for bad in ("../options.json", "2026-11-11.md/../../x", "x.md"):
            self.assertFalse(REPORT_RE.match(bad))
        self.assertEqual(report_label("2026-11-11-130500.md"), "Wed Nov 11, 1:05 PM")


if __name__ == "__main__":
    unittest.main()
