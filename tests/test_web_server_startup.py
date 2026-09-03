import os
import sys
from types import SimpleNamespace

import uvicorn
import pytest

import web_server


class FakePopen:
    def __init__(self):
        self.pid = 4242
        self.terminated = False
        self.killed = False

    def poll(self):
        return None if not self.terminated else 0

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        return 0 if self.terminated else None

    def kill(self):
        self.killed = True
        self.terminated = True


@pytest.fixture(autouse=True)
def reset_mcp_process(monkeypatch):
    monkeypatch.setattr(web_server, "_mcp_process", None)
    yield
    web_server._mcp_process = None


def test_start_mcp_server_builds_expected_command(monkeypatch):
    calls = {}

    def fake_popen(command, **kwargs):
        calls["command"] = command
        calls["kwargs"] = kwargs
        return FakePopen()

    monkeypatch.setattr(web_server.subprocess, "Popen", fake_popen)
    process = web_server.start_mcp_server("127.0.0.1", 9876)

    assert process is not None and process.pid == 4242
    assert calls["command"][0] == sys.executable
    assert calls["command"][1].endswith("mcp_server.py")
    assert calls["command"][2:] == ["--http", "--host", "127.0.0.1", "--port", "9876"]
    assert calls["kwargs"]["cwd"] == os.path.dirname(calls["command"][1])
    assert web_server._mcp_process is process


def test_start_mcp_server_can_be_disabled(monkeypatch):
    monkeypatch.setenv("SSHELPER_START_MCP", "0")

    def fail_if_called(*args, **kwargs):
        pytest.fail("MCP process should not start when disabled")

    monkeypatch.setattr(
        web_server.subprocess,
        "Popen",
        fail_if_called,
    )

    assert web_server.start_mcp_server() is None


def test_stop_mcp_server_terminates_once(monkeypatch):
    process = FakePopen()
    monkeypatch.setattr(web_server, "_mcp_process", process)

    web_server.stop_mcp_server()

    assert process.terminated
    assert web_server._mcp_process is None


def test_mcp_process_is_reused(monkeypatch):
    process = FakePopen()
    monkeypatch.setattr(web_server, "_mcp_process", process)

    def fail_if_called(*args, **kwargs):
        pytest.fail("An active MCP process should not be started twice")

    monkeypatch.setattr(
        web_server.subprocess,
        "Popen",
        fail_if_called,
    )

    assert web_server.start_mcp_server() is process


def test_web_main_starts_both_servers(monkeypatch):
    calls = SimpleNamespace(mcp=None, uvicorn=0, stopped=False)
    monkeypatch.setenv("WEB_HOST", "127.0.0.1")
    monkeypatch.setenv("WEB_PORT", "18000")
    monkeypatch.setenv("MCP_HOST", "127.0.0.1")
    monkeypatch.setenv("MCP_PORT", "18787")
    monkeypatch.setattr(
        web_server,
        "start_mcp_server",
        lambda host, port: calls.__setattr__("mcp", (host, port)),
    )
    monkeypatch.setattr(
        uvicorn,
        "run",
        lambda *args, **kwargs: calls.__setattr__("uvicorn", kwargs.copy()),
    )
    monkeypatch.setattr(
        web_server,
        "stop_mcp_server",
        lambda: calls.__setattr__("stopped", True),
    )

    web_server.main()

    assert calls.mcp == ("127.0.0.1", 18787)
    assert calls.uvicorn["host"] == "127.0.0.1"
    assert calls.uvicorn["port"] == 18000
    assert calls.uvicorn["reload"] is True
    assert calls.stopped is True
