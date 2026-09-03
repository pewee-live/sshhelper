"""Unit tests for the MCP persistent-session runner (no real SSH needed)."""
import sys
from types import SimpleNamespace

import pytest

sys.path.insert(0, __file__.rsplit("\\", 2)[0] if "\\" in __file__ else ".")

import mcp_connections as mc


def no_audit(**kwargs):
    return None


class FakeChannel:
    """Duck-typed paramiko channel with a scripted output queue."""

    def __init__(self, chunks=None):
        self.chunks = list(chunks or [])
        self.sent = []
        self.closed = False
        self.exit_ready = False
        self.exit_status = None
        self.pty_terms = []
        self.exec_commands = []

    def recv_ready(self):
        return bool(self.chunks)

    def recv(self, n):
        if not self.chunks:
            raise EOFError("no data")
        chunk = self.chunks.pop(0)
        if isinstance(chunk, Exception):
            raise chunk
        return chunk

    def recv_stderr_ready(self):
        return False

    def recv_stderr(self, n):
        return b""

    def exit_status_ready(self):
        return self.exit_ready

    def recv_exit_status(self):
        return self.exit_status

    def sendall(self, data):
        self.sent.append(data)

    def close(self):
        self.closed = True

    def get_pty(self, term=None, width=None, height=None):
        self.pty_terms.append((term, width, height))

    def exec_command(self, command):
        self.exec_commands.append(command)


class FakeTransport:
    def __init__(self, channel):
        self.channel = channel
        self.keepalive = None

    def is_active(self):
        return True

    def open_session(self):
        return self.channel

    def set_keepalive(self, seconds):
        self.keepalive = seconds


class FakeSSHClient:
    def __init__(self, channel):
        self.transport = FakeTransport(channel)

    def get_transport(self):
        return self.transport


class FakeSerial:
    def __init__(self, chunks=None):
        self.incoming = bytearray(b"".join(chunks or []))
        self.written = []

    @property
    def in_waiting(self):
        return len(self.incoming)

    def read(self, size):
        data = bytes(self.incoming[:size])
        del self.incoming[:size]
        return data

    def write(self, data):
        self.written.append(bytes(data))
        if data.startswith(b"version"):
            self.incoming.extend(b"U-Boot> version\r\n")
        elif data == b"\r\n":
            self.incoming.extend(b"login: ")

    def reset_input_buffer(self):
        self.incoming.clear()


class FakeDeviceManager:
    def __init__(self):
        self.connections = {}
        self.connect_calls = []
        self.disconnect_calls = []
        self.upload_calls = []
        self.download_calls = []
        self.reconnect_calls = []
        self.reconnect_result = True
        self.next_channel = None
        self.connect_serial_calls = []
        self.next_serial = None

    def connect_ssh(self, host, username, password=None, port=22, session_id=None):
        self.connect_calls.append((host, username, password, port, session_id))
        channel = self.next_channel or FakeChannel()
        self.connections[session_id] = SimpleNamespace(
            conn_type="ssh", ssh_client=FakeSSHClient(channel),
        )
        return "connected"

    def connect_serial(self, port, baudrate=115200, session_id=None):
        self.connect_serial_calls.append((port, baudrate, session_id))
        serial_client = self.next_serial or FakeSerial()
        self.connections[session_id] = SimpleNamespace(
            conn_type="serial", serial_client=serial_client,
        )
        return "connected"

    def get_connection(self, session_id=None):
        return self.connections[session_id]

    def disconnect(self, session_id=None):
        self.disconnect_calls.append(session_id)

    def reconnect(self, session_id=None, timeout=120.0, poll_interval=5.0):
        self.reconnect_calls.append((session_id, timeout, poll_interval))
        return self.reconnect_result

    def upload_file(self, local_path, remote_path, session_id=None):
        self.upload_calls.append((local_path, remote_path, session_id))
        return "uploaded"

    def download_file(self, remote_path, local_path, session_id=None):
        self.download_calls.append((remote_path, local_path, session_id))
        return "downloaded"


def make_command(channel, command="uname -a", **kwargs):
    ssh = FakeSSHClient(channel)
    return mc.PtyCommand(ssh, command, "c1", audit_record=no_audit, **kwargs)


class TestPtyCommand:
    def test_completed_command(self):
        ch = FakeChannel([b"hello\n", b"world\n"])
        ch.exit_ready = True
        ch.exit_status = 0
        cmd = make_command(ch)
        assert cmd.poll(1) == "completed"
        assert cmd.exit_status == 0
        assert "hello" in cmd.output and "world" in cmd.output
        assert cmd.take_new_output() == "hello\nworld\n"
        assert cmd.take_new_output() == ""
        assert ch.closed
        assert ch.pty_terms[0][0] == "xterm-256color"

    def test_exit_waits_for_pending_output(self):
        ch = FakeChannel([b"data\n"])
        ch.exit_ready = True
        ch.exit_status = 0
        cmd = make_command(ch)
        assert cmd.poll(1) == "completed"
        assert "data" in cmd.output

    def test_backspace_spinner_is_not_accumulated(self):
        ch = FakeChannel([b"-\x08\\\x08|"])
        ch.exit_ready = True
        ch.exit_status = 0
        cmd = make_command(ch)
        assert cmd.poll(1) == "completed"
        assert cmd.output.strip() == "|"

    def test_password_prompt_awaits_input(self):
        ch = FakeChannel([b"[sudo] password: "])
        cmd = make_command(ch, "sudo true")
        state = cmd.poll(1.0)
        assert state == "awaiting_input"
        assert cmd.hint == "password"

        cmd.send("secret")
        assert ch.sent == [b"secret\n"]
        assert cmd.state == "running"

        ch.chunks.append(b"ok\n")
        ch.exit_ready = True
        ch.exit_status = 0
        assert cmd.poll(1) == "completed"
        assert "ok" in cmd.output

    def test_confirm_prompt(self):
        ch = FakeChannel([b"Continue? [y/n] "])
        cmd = make_command(ch, "apt install x")
        assert cmd.poll(1.0) == "awaiting_input"
        assert cmd.hint == "confirm"

    def test_timeout_returns_running(self):
        ch = FakeChannel()
        cmd = make_command(ch, "sleep 100")
        assert cmd.poll(0.3) == "running"
        assert cmd.is_terminal is False

    def test_stop_sends_ctrl_c_and_closes(self):
        ch = FakeChannel()
        cmd = make_command(ch, "sleep 100")
        cmd.stop()
        assert cmd.state == "stopped"
        assert ch.sent == [b"\x03"]
        assert ch.closed

    def test_channel_error_fails(self):
        ch = FakeChannel([RuntimeError("boom")])
        cmd = make_command(ch)
        assert cmd.poll(1) == "failed"
        assert "boom" in cmd.error

    def test_closed_channel_grace_then_fail(self):
        ch = FakeChannel()
        ch.closed = True
        cmd = make_command(ch, closed_grace=0.1)
        assert cmd.poll(0.5) == "failed"
        assert "closed" in cmd.error.lower()

    def test_new_output_is_incremental(self):
        ch = FakeChannel([b"first\n"])
        cmd = make_command(ch)
        cmd.poll(0)
        assert cmd.take_new_output() == "first\n"
        assert cmd.take_new_output() == ""
        ch.chunks.append(b"second\n")
        cmd.poll(0)
        assert cmd.take_new_output() == "second\n"

    def test_output_memory_is_bounded(self):
        ch = FakeChannel([b"x" * 1000] * 300)
        ch.exit_ready = True
        ch.exit_status = 0
        cmd = make_command(ch)
        cmd.poll(1)
        assert len(cmd.output) <= mc.OUTPUT_MEMORY_LIMIT


class TestConnectionRegistry:
    def make_registry(self):
        dm = FakeDeviceManager()
        return mc.ConnectionRegistry(device_manager=dm), dm

    def test_connect_creates_session_and_keepalive(self):
        reg, dm = self.make_registry()
        conn = reg.connect("192.168.1.50", "root", "pw", 22)
        host, user, pwd, port, sid = dm.connect_calls[0]
        assert (host, user, pwd, port) == ("192.168.1.50", "root", "pw", 22)
        assert sid == conn.session_id and sid.startswith("mcp-192.168.1.50-")
        assert dm.get_connection(sid).ssh_client.transport.keepalive == 15

    def test_connect_serial_creates_session(self):
        reg, dm = self.make_registry()
        dm.next_serial = FakeSerial([b"U-Boot 2024.01\r\n"])
        conn = reg.connect_serial("/dev/ttyUSB0", 1500000)
        assert dm.connect_serial_calls == [("/dev/ttyUSB0", 1500000, conn.session_id)]
        assert conn.session_id.startswith("mcp-serial--dev-ttyUSB0-")
        assert conn.target == "serial:/dev/ttyUSB0"
        assert "serial /dev/ttyUSB0@1500000" in reg.status_text()

    def test_connect_replaces_previous_session(self):
        reg, dm = self.make_registry()
        first = reg.connect("1.1.1.1")
        second = reg.connect("2.2.2.2")
        assert first.session_id in dm.disconnect_calls
        assert reg.active().session_id == second.session_id

    def test_require_without_connection(self):
        reg, _ = self.make_registry()
        with pytest.raises(mc.NotConnectedError):
            reg.require()

    def test_start_command_firewall(self):
        reg, dm = self.make_registry()
        reg.connect("1.1.1.1")
        with pytest.raises(mc.SafetyFirewallError):
            reg.start_command("htop")
        with pytest.raises(mc.SafetyFirewallError):
            reg.start_command("sensors-detect")

    def test_command_lifecycle_and_busy(self):
        reg, dm = self.make_registry()
        reg.connect("1.1.1.1")
        cmd = reg.start_command("uname -a")
        assert cmd.command_id == "c1"
        with pytest.raises(mc.CommandBusyError):
            reg.start_command("uptime")

        cmd.channel.chunks.append(b"out\n")
        cmd.channel.exit_ready = True
        cmd.channel.exit_status = 0
        assert cmd.poll(1) == "completed"
        reg.note_finished(cmd)
        reg.note_finished(cmd)  # idempotent
        assert len(reg.active().history) == 1

        cmd2 = reg.start_command("uptime")
        assert cmd2.command_id == "c2"
        assert reg.find_command("c2") is cmd2
        assert reg.find_command("") is cmd2
        assert reg.find_command("c1") is cmd

    def test_serial_command_completes_without_exit_status(self):
        serial = FakeSerial([b"U-Boot> version\r\n"])
        cmd = mc.SerialCommand(
            serial, "version", "c1", audit_record=no_audit, idle_done=0.2,
        )
        assert serial.written == [b"version\r\n"]
        assert cmd.poll(1) == "completed"
        assert cmd.exit_status is None
        assert "U-Boot> version" in cmd.output

    def test_serial_prompt_awaits_input_and_uses_crlf(self):
        serial = FakeSerial([b"login: "])
        cmd = mc.SerialCommand(
            serial, "", "c1", audit_record=no_audit, idle_done=1.0,
        )
        assert cmd.poll(1) == "awaiting_input"
        assert cmd.hint == "password"
        cmd.send("root")
        assert serial.written[-1] == b"root\r\n"
        assert cmd.state == "running"

    def test_serial_stop_sends_ctrl_c_without_closing_port(self):
        serial = FakeSerial()
        cmd = mc.SerialCommand(serial, "watch", "c1", audit_record=no_audit)
        cmd.stop()
        assert cmd.state == "stopped"
        assert serial.written[-1] == b"\x03"

    def test_serial_complete_is_read_only(self):
        serial = FakeSerial([b"boot...\r\n"])
        cmd = mc.SerialCommand(serial, "<console read>", "c1",
                               audit_record=no_audit, send_line=False,
                               idle_done=1.0)
        assert cmd.poll(0.1) == "running"
        cmd.complete()
        assert cmd.state == "completed"
        assert "boot" in cmd.output
        assert serial.written == []

    def test_serial_registry_dispatch_and_console_read(self):
        reg, dm = self.make_registry()
        serial = FakeSerial([b"boot log\r\n"])
        dm.next_serial = serial
        reg.connect_serial("COM3")
        cmd = reg.start_command("uname -a")
        assert isinstance(cmd, mc.SerialCommand)
        assert serial.written == [b"uname -a\r\n"]
        cmd.poll(3)
        reg.note_finished(cmd)

        read_cmd = reg.start_console_read()
        assert read_cmd.command == "<console read>"
        assert serial.written == [b"uname -a\r\n"]

    def test_upload_serial_returns_clear_error(self):
        reg, dm = self.make_registry()
        dm.next_serial = FakeSerial()
        reg.connect_serial("COM3")
        result = reg.upload("local.bin", "/tmp/remote.bin")
        assert "only supported over SSH" in result

    def test_upload_download_use_session(self):
        reg, dm = self.make_registry()
        conn = reg.connect("1.1.1.1")
        assert reg.upload("local.bin", "/tmp/remote.bin") == "uploaded"
        assert dm.upload_calls == [("local.bin", "/tmp/remote.bin", conn.session_id)]
        assert reg.download("/tmp/remote.bin", "local.bin") == "downloaded"
        assert dm.download_calls == [("/tmp/remote.bin", "local.bin", conn.session_id)]

    def test_disconnect_stops_active_command(self):
        reg, dm = self.make_registry()
        conn = reg.connect("1.1.1.1")
        cmd = reg.start_command("sleep 100")
        reg.disconnect()
        assert cmd.state == "stopped"
        assert conn.session_id in dm.disconnect_calls
        assert reg.active() is None
        assert "Not connected" in reg.status_text()

    def test_reconnect_keeps_session(self):
        reg, dm = self.make_registry()
        conn = reg.connect("1.1.1.1")
        reg.start_command("sleep 100")
        assert reg.reconnect(timeout=5, poll_interval=1) is True
        assert dm.reconnect_calls == [(conn.session_id, 5, 1)]
        assert reg.active().session_id == conn.session_id
        assert reg.active().command is None

    def test_reconnect_failure_clears(self):
        reg, dm = self.make_registry()
        conn = reg.connect("1.1.1.1")
        dm.reconnect_result = False
        assert reg.reconnect(timeout=1) is False
        assert reg.active() is None

    def test_status_shows_active_command(self):
        reg, dm = self.make_registry()
        reg.connect("1.1.1.1")
        cmd = reg.start_command("sleep 100")
        text = reg.status_text()
        assert "1.1.1.1" in text
        assert "c1 [running] 'sleep 100'" in text


class TestPromptDetection:
    def test_password_variants(self):
        assert mc._classify_prompt("[sudo] password:") == "password"
        assert mc._classify_prompt("Password for root:") == "password"
        assert mc._classify_prompt("密码：") == "password"

    def test_confirm_variants(self):
        assert mc._classify_prompt("Do you want to continue? [y/N]") == "confirm"
        assert mc._classify_prompt("Press enter to accept") == "confirm"

    def test_not_a_prompt(self):
        assert mc._classify_prompt("total 42") is None
        assert mc._classify_prompt("") is None
