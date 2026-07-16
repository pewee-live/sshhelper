"""Tests for case_generator.py: frontmatter parsing and markdown rendering."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

os.environ.setdefault("OPENAI_API_KEY", "test-key")
from case_generator import _case_to_markdown, _parse_frontmatter, _slugify


class TestSlugify:
    def test_basic(self):
        assert _slugify("Install Docker on Ubuntu") == "install-docker-on-ubuntu"

    def test_special_chars(self):
        assert _slugify("Fix: Network Error!") == "fix-network-error"

    def test_chinese(self):
        result = _slugify("配置 zram")
        assert len(result) > 0

    def test_empty(self):
        assert _slugify("") == "untitled"


class TestCaseMarkdown:
    def _mock_case(self):
        return {
            "title": "Test Case",
            "domain": "network",
            "platform": "Ubuntu 24.04",
            "tags": ["iptables", "firewall"],
            "search_queries": ["how to check firewall", "iptables block"],
            "error_message": "Connection refused",
            "symptom": "Cannot connect to port 80",
            "prerequisites": "Root access required",
            "diagnosis": "Checked iptables rules",
            "root_cause": "Firewall blocking port 80",
            "solution": "Open port 80",
            "verification": "curl localhost:80 succeeds",
            "rollback": "Close port 80",
            "risk": "Exposes service externally",
            "qa": [{"q": "Why port 80?", "a": "HTTP default"}],
            "session_id": "test-session",
            "generated_at": "2026-07-16T00:00:00",
        }

    def test_markdown_has_all_sections(self):
        md = _case_to_markdown(self._mock_case())
        for section in ["## Symptom", "## Error Message", "## Prerequisites",
                        "## Diagnosis", "## Root Cause", "## Solution",
                        "## Verification", "## Rollback", "## Risk & Considerations",
                        "## Common Questions"]:
            assert section in md, f"Missing section: {section}"

    def test_markdown_has_frontmatter(self):
        md = _case_to_markdown(self._mock_case())
        assert md.startswith("---")
        assert "title:" in md
        assert "domain:" in md
        assert "tags:" in md

    def test_frontmatter_roundtrip(self):
        md = _case_to_markdown(self._mock_case())
        meta = _parse_frontmatter(md)
        assert meta["title"] == "Test Case"
        assert meta["domain"] == "network"
        assert "iptables" in meta["tags"]
        assert "firewall" in meta["tags"]
        assert len(meta["search_queries"]) == 2

    def test_frontmatter_empty_tags(self):
        case = self._mock_case()
        case["tags"] = []
        case["search_queries"] = []
        md = _case_to_markdown(case)
        meta = _parse_frontmatter(md)
        assert meta["tags"] == []
        assert meta["search_queries"] == []