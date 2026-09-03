"""Tests for browser-bound terminal output normalization."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import web_server


def test_replacement_snapshot_collapses_replayed_log_events(monkeypatch):
    monkeypatch.setitem(web_server.session_events, "sess", [])
    monkeypatch.setitem(web_server.session_viewers, "sess", None)
    monkeypatch.setattr(web_server, "main_loop", None)

    web_server.web_on_output("old frame\n", "sess")
    web_server.web_on_output("final frame\n", "sess", replace=True)

    events = web_server.session_events["sess"]
    assert len(events) == 1
    assert events[0]["type"] == "log"
    assert events[0]["replace"] is True
    assert events[0]["content"] == "final frame\n"


def test_oversized_browser_log_event_is_truncated(monkeypatch):
    monkeypatch.setitem(web_server.session_events, "sess", [])
    monkeypatch.setitem(web_server.session_viewers, "sess", None)
    monkeypatch.setattr(web_server, "main_loop", None)

    web_server.web_on_output("x" * (web_server.MAX_LOG_EVENT_CHARS + 100), "sess")
    event = web_server.session_events["sess"][-1]
    assert len(event["content"]) <= web_server.MAX_LOG_EVENT_CHARS + 100
    assert "characters truncated" in event["content"]
