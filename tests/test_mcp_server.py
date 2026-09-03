"""Tests for the MCP tool wrappers and MCP protocol wiring (in-memory)."""
import asyncio
import sys
from types import SimpleNamespace

sys.path.insert(0, __file__.rsplit("\\", 2)[0] if "\\" in __file__ else ".")

import mcp_server
from mcp_connections import CommandBusyError, NotConnectedError, SafetyFirewallError


class FakeCmd:
    def __init__(self, state="completed", exit_status=None, output="ok",
                 hint=None, command="fake cmd", command_id="c1", error=None):
        self.state = state
        self.exit_status = exit_status
        self.output = output
        self.hint = hint
        self.command = command
        self.command_id = command_id
        self.error = error
        self.sent = []
        self._new_output = output

    @property
    def is_terminal(self):
        return self.state in ("completed", "failed", "stopped")

    def poll(self, timeout=0):
        return self.state

    def send(self, text, press_enter=True):
        self.sent.append((text, press_enter))

    def stop(self):
        self.state = "stopped"

    def complete(self):
        self.state = "completed"

    def take_new_output(self):
        out = self._new_output
        self._new_output = ""
        return out

    def tail(self, lines=80):
        return self.output[-lines:]


class FakeRegistry:
    def __init__(self, cmd=None):
        self.cmd = cmd or FakeCmd()
        self.calls = []

    def connect(self, host, username, password, port):
        self.calls.append(("connect", host, username, port))
        return SimpleNamespace(session_id="mcp-fake")

    def connect_serial(self, port, baudrate):
        self.calls.append(("connect-serial", port, baudrate))
        return SimpleNamespace(
            session_id="mcp-serial-fake",
            conn_type="serial",
            serial_port=port,
            baudrate=baudrate,
        )

    def disconnect(self):
        self.calls.append(("disconnect",))

    def status_text(self):
        return "Not connected. Call connect(host, username, port) first."

    def start_command(self, command):
        self.calls.append(("start", command))
        return self.cmd

    def start_console_read(self):
        self.calls.append(("read-console",))
        return self.cmd

    def find_command(self, command_id=""):
        self.calls.append(("find", command_id))
        return self.cmd

    def note_finished(self, cmd):
        self.calls.append(("note", cmd.command_id))

    def upload(self, local_path, remote_path):
        self.calls.append(("upload", local_path, remote_path))
        return "uploaded ok"

    def download(self, remote_path, local_path):
        self.calls.append(("download", remote_path, local_path))
        return "downloaded ok"

    def require(self):
        self.calls.append(("require",))
        return SimpleNamespace(
            session_id="mcp-fake",
            conn_type="ssh",
            target="ssh:fake",
            host="fake",
        )

    def reconnect(self, timeout=90.0, poll_interval=5.0):
        self.calls.append(("reconnect", timeout))
        return True


class TestRunTool:
    def test_not_connected(self, monkeypatch):
        reg = FakeRegistry()
        def start(command):
            raise NotConnectedError("No active connection. Call connect(host) first.")
        reg.start_command = start
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.run("uname -a")
        assert result.startswith("Error:")
        assert "connect" in result

    def test_completed(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", exit_status=0, output="Linux board"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.run("uname -a")
        assert "[c1 completed exit=0]" in result
        assert "Linux board" in result

    def test_busy_reports_active_command(self, monkeypatch):
        reg = FakeRegistry()
        active = FakeCmd(state="running", command="sleep 100")
        def start(command):
            raise CommandBusyError(active)
        reg.start_command = start
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.run("uptime")
        assert "still active" in result
        assert "[c1 running]" in result
        assert "sleep 100" in result

    def test_firewall(self, monkeypatch):
        reg = FakeRegistry()
        def start(command):
            raise SafetyFirewallError("Error: htop is blocked")
        reg.start_command = start
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.run("htop")
        assert "htop" in result and result.startswith("Error")

    def test_awaiting_password_guidance(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="awaiting_input", hint="password", output="[sudo] password:"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.run("sudo apt update")
        assert "awaiting_input" in result
        assert "send_input" in result


class TestInteractiveTools:
    def test_send_input_returns_new_output(self, monkeypatch):
        cmd = FakeCmd(state="awaiting_input", hint="password", output="[sudo] password:")
        reg = FakeRegistry(cmd)
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        cmd.state = "running"  # send() flips it
        result = mcp_server.send_input("secret", wait_seconds=0)
        assert cmd.sent == [("secret", True)]
        assert "[c1 after send_input]" in result
        assert "NEW OUTPUT" in result

    def test_send_input_without_command(self, monkeypatch):
        reg = FakeRegistry()
        reg.find_command = lambda command_id="": None
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.send_input("y")
        assert result.startswith("Error")

    def test_get_output_unknown_id(self, monkeypatch):
        reg = FakeRegistry()
        reg.find_command = lambda command_id="": None
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        assert "Unknown command_id" in mcp_server.get_output("nope")

    def test_get_output_running_no_new_output(self, monkeypatch):
        cmd = FakeCmd(state="running", output="building...")
        cmd.take_new_output()  # drain the initial output
        reg = FakeRegistry(cmd)
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.get_output("c1", wait_seconds=0)
        assert "[c1 running]" in result
        assert "still running" in result
        assert "building" in result

    def test_stop_already_completed(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        assert "already completed" in mcp_server.stop_command("c1")


class TestPersistentToolWiring:
    def test_status_uses_registry(self, monkeypatch):
        reg = FakeRegistry()
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        assert mcp_server.status() == reg.status_text()

    def test_upload_uses_persistent_session(self, monkeypatch, tmp_path):
        local = tmp_path / "f.bin"
        local.write_bytes(b"x")
        reg = FakeRegistry()
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.upload_file(str(local), "/tmp/f.bin")
        assert result == "uploaded ok"
        assert ("upload", str(local), "/tmp/f.bin") in reg.calls

    def test_upload_missing_local_file(self, monkeypatch):
        reg = FakeRegistry()
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        assert "not found" in mcp_server.upload_file("missing.bin", "/tmp/x")

    def test_download_uses_persistent_session(self, monkeypatch):
        reg = FakeRegistry()
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        assert mcp_server.download_file("/tmp/f.bin", "local.bin") == "downloaded ok"
        assert ("download", "/tmp/f.bin", "local.bin") in reg.calls

    def test_connect_with_verify(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", exit_status=0,
                                   output="Linux 6.1 aarch64\nmyboard"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.connect("1.2.3.4", username="root", password="pw")
        assert "Connected: root@1.2.3.4:22" in result
        assert "Linux 6.1" in result and "myboard" in result
        assert ("connect", "1.2.3.4", "root", 22) in reg.calls

    def test_connect_verify_failure_disconnects(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", exit_status=1, output="bad"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.connect("1.2.3.4", username="root", password="pw")
        assert result.startswith("Error")
        assert ("disconnect",) in reg.calls

    def test_connect_serial_with_initial_console_output(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", output="U-Boot 2024.01"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.connect_serial("/dev/ttyUSB0", 1500000, listen_seconds=0)
        assert "Connected: serial /dev/ttyUSB0@1500000" in result
        assert "no exit status" in result
        assert ("connect-serial", "/dev/ttyUSB0", 1500000) in reg.calls

    def test_read_console_uses_read_only_command(self, monkeypatch):
        cmd = FakeCmd(state="completed", output="Starting kernel...")
        reg = FakeRegistry(cmd)
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        result = mcp_server.read_console(wait_seconds=0)
        assert "[c1 completed]" in result
        assert "Starting kernel..." in result
        assert ("read-console",) in reg.calls

    def test_reboot_uses_registry(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", exit_status=0, output="alive\nup 1 min"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        fake_dm = SimpleNamespace(
            get_connection=lambda sid: SimpleNamespace(
                ssh_client=SimpleNamespace(exec_command=lambda c, timeout=None: None),
            ),
        )
        monkeypatch.setattr(mcp_server, "DEVICE_MANAGER", fake_dm)
        result = mcp_server.reboot_device(wait_seconds=5)
        assert "rebooted and reconnected" in result
        assert ("reconnect", 5) in reg.calls

    def test_snapshot_uses_registry(self, monkeypatch):
        reg = FakeRegistry(FakeCmd(state="completed", exit_status=0, output="probe data"))
        monkeypatch.setattr(mcp_server, "REGISTRY", reg)
        fake_baseline = SimpleNamespace(save_snapshot=lambda host, probes: {"timestamp": "T1"})
        monkeypatch.setattr(mcp_server, "BASELINE_MANAGER", fake_baseline)
        result = mcp_server.snapshot_config()
        assert "Snapshot saved at T1" in result
        assert any(c[0] == "start" for c in reg.calls)  # probes ran through the registry


class TestMcpProtocol:
    def test_tool_discovery_and_status_call(self, monkeypatch):
        monkeypatch.setattr(mcp_server, "REGISTRY", FakeRegistry())

        async def scenario():
            from fastmcp import Client
            async with Client(mcp_server.mcp) as client:
                tools = await client.list_tools()
                names = {t.name for t in tools}
                expected = {
                    "connect", "run", "send_input", "get_output", "stop_command",
                    "status", "disconnect", "upload_file", "download_file",
                    "list_serial_ports", "connect_serial", "read_console",
                    "reboot_device", "snapshot_config", "execute_command",
                    "list_snapshots", "get_snapshot", "list_device_profiles",
                }
                assert expected <= names
                result = await client.call_tool("status", {})
                return result

        result = asyncio.run(scenario())
        text = str(getattr(result, "data", None) or getattr(result, "content", result))
        assert "connected" in text.lower()


class TestOneShotCommandSafety:
    def test_execute_command_uses_firewall(self, monkeypatch):
        calls = []
        monkeypatch.setattr(
            mcp_server, "safety_firewall_error",
            lambda command: "Error: Command rejected by safety firewall.",
        )
        monkeypatch.setattr(
            mcp_server.audit, "record",
            lambda **kwargs: calls.append(kwargs),
        )
        def fail_if_called(*args, **kwargs):
            raise AssertionError("blocked command should not reach SSH")
        monkeypatch.setattr(mcp_server, "_exec_ssh", fail_if_called)

        result = mcp_server.execute_command("192.0.2.10", "htop")
        assert result == "Error: Command rejected by safety firewall."
        assert calls == [{
            "session_id": None,
            "device": "ssh:192.0.2.10",
            "command": "htop",
            "source": "mcp",
            "detail": "blocked by safety firewall",
        }]


class TestBaselineTools:
    def test_list_snapshots(self, monkeypatch):
        fake = SimpleNamespace(list_snapshots=lambda host: [{
            "timestamp": "20260101_010101",
            "datetime": "2026-01-01T01:01:01",
            "probe_count": 2,
            "probe_names": ["ip_addr", "uname"],
        }])
        monkeypatch.setattr(mcp_server, "BASELINE_MANAGER", fake)
        result = mcp_server.list_snapshots("192.0.2.10")
        assert "Found 1 snapshot(s)" in result
        assert "20260101_010101" in result
        assert "ip_addr, uname" in result

    def test_get_snapshot_single_probe(self, monkeypatch):
        fake = SimpleNamespace(get_snapshot=lambda host, ts: {
            "device_key": host,
            "timestamp": ts,
            "datetime": "2026-01-01T01:01:01",
            "probes": {"ip_addr": "1: lo", "uname": "Linux"},
        })
        monkeypatch.setattr(mcp_server, "BASELINE_MANAGER", fake)
        result = mcp_server.get_snapshot("board", "T1", probe="ip_addr")
        assert "=== ip_addr ===" in result
        assert "1: lo" in result
        assert "Linux" not in result


class TestKnowledgeBaseSearch:
    def test_token_overlap_finds_cases(self, monkeypatch, tmp_path):
        import case_generator
        case_file = tmp_path / "case.md"
        case_file.write_text("docker bridge does not expose an IP route", encoding="utf-8")
        monkeypatch.setattr(case_generator, "list_cases", lambda: [{
            "domain": "linux",
            "title": "Container networking failure",
            "path": str(case_file),
            "tags": ["docker", "network"],
            "search_queries": ["docker no route"],
        }])
        result = mcp_server.search_kb("docker networking broken route")
        assert "Found 1 matching case(s)" in result
        assert "Container networking failure" in result


class TestDeviceProfileTools:
    def test_list_device_profiles(self, monkeypatch):
        fake = SimpleNamespace(list_profiles=lambda: [{
            "device_key": "192.0.2.10",
            "hostname": "board",
            "os": "Debian 13",
            "kernel": "6.6.0",
            "architecture": "aarch64",
            "updated_at": "2026-01-01T00:00:00",
        }])
        monkeypatch.setattr(mcp_server, "DEVICE_PROFILE_MANAGER", fake)
        result = mcp_server.list_device_profiles()
        assert "Device Profiles: 1" in result
        assert "192.0.2.10: board | Debian 13 | 6.6.0 | aarch64" in result
