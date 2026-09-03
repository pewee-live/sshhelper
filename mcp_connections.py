"""Persistent SSH sessions for the MCP server.

The MCP tools need different execution semantics than the Web/CLI engine:
tool calls are short-lived request/response cycles, so a command must be
startable, pollable and interactive-input-capable without blocking inside a
websocket callback loop. This module implements that thin runner while reusing
the shared terminal cleaner, prompt detectors, safety firewall and audit trail
from tools.py.
"""
from __future__ import annotations

import atexit
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from tools import (
    DEVICE_MANAGER,
    PASSWORD_PROMPT_KEYWORDS,
    PROMPT_DETECT_MIN_IDLE,
    INTERACTIVE_PROMPT_PATTERNS,
    TerminalOutputFilter,
    safety_firewall_error,
)
import audit

# Never hold more than this much cleaned output per command in memory.
OUTPUT_MEMORY_LIMIT = 256 * 1024
OUTPUT_TRIM_TO = 128 * 1024
CLOSED_STATUS_GRACE = 2.0


class NotConnectedError(RuntimeError):
    """Raised when a tool needs the default connection but none exists."""


class CommandBusyError(RuntimeError):
    """Raised when starting a command while another one is still active."""

    def __init__(self, active):
        self.active = active
        super().__init__(
            f"Command {active.command_id} is still active (state={active.state}). "
            "Finish it first with get_output(), send_input() or stop_command()."
        )


class SafetyFirewallError(RuntimeError):
    """Raised when the shared safety firewall rejects a command."""


def _last_line(buffer: str) -> str:
    lines = buffer.split("\n")
    return lines[-1].strip() if lines else ""


def _classify_prompt(line: str) -> Optional[str]:
    """Classify a stalled PTY line as a password or confirmation prompt."""
    if not line:
        return None
    low = line.lower()
    if any(kw.lower() in low for kw in PASSWORD_PROMPT_KEYWORDS):
        return "password"
    if any(pat.lower() in low for pat in INTERACTIVE_PROMPT_PATTERNS):
        return "confirm"
    return None


class PtyCommand:
    """One non-blocking, PTY-backed remote command.

    Lifecycle: running -> completed | failed | stopped, possibly passing
    through awaiting_input when sudo / apt / fdisk asks for input.
    """

    def __init__(self, ssh_client, command: str, command_id: str,
                 session_id: str = "mcp", device: str = "unknown",
                 audit_record: Optional[Callable] = None,
                 closed_grace: float = CLOSED_STATUS_GRACE):
        transport = ssh_client.get_transport()
        if transport is None or not transport.is_active():
            raise ConnectionError("SSH transport is not active. Reconnect first.")
        channel = transport.open_session()
        channel.get_pty(term="xterm-256color", width=160, height=48)
        channel.exec_command(command)

        self.channel = channel
        self.command = command
        self.command_id = command_id
        self.session_id = session_id
        self.device = device
        self.state = "running"  # running|awaiting_input|completed|failed|stopped
        self.hint: Optional[str] = None
        self.exit_status: Optional[int] = None
        self.error: Optional[str] = None
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self._output = ""
        self._terminal = TerminalOutputFilter()
        self._undelivered = 0
        self._recent = ""
        self._idle = 0.0
        self._closed_since: Optional[float] = None
        self._closed_grace = closed_grace
        self._audit_record = audit_record or audit.record
        self._noted = False
        self._audit_record(session_id=session_id, device=device,
                           command=command, source="mcp")

    # --- Output helpers ----------------------------------------------------

    @property
    def output(self) -> str:
        """Full cleaned output (bounded by OUTPUT_MEMORY_LIMIT)."""
        return self._output

    def take_new_output(self) -> str:
        """Return and mark-as-delivered output produced since the last call."""
        new = self._output[self._undelivered:]
        self._undelivered = len(self._output)
        return new

    def tail(self, lines: int = 80) -> str:
        return self._terminal.tail(lines).strip()

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    # --- Core loop ---------------------------------------------------------

    def _drain_once(self) -> bool:
        """Read any immediately available output. Returns True if data came."""
        got_data = False
        for is_stderr in (False, True):
            ready = self.channel.recv_stderr_ready() if is_stderr else self.channel.recv_ready()
            while ready:
                try:
                    chunk_bytes = (self.channel.recv_stderr(4096) if is_stderr
                                   else self.channel.recv(4096))
                except Exception as e:
                    self._fail(f"Error reading channel: {e}")
                    return got_data
                if not chunk_bytes:
                    break
                ready = (self.channel.recv_stderr_ready() if is_stderr
                         else self.channel.recv_ready())
                self._terminal.push(chunk_bytes.decode("utf-8", errors="replace"))
                self._idle = 0.0
                chunk = self._terminal.take_stable()
                if not chunk:
                    continue
                got_data = True
                self._append(chunk)
        return got_data

    def _append(self, chunk: str) -> None:
        self._output += chunk
        self._recent = (self._recent + chunk)[-4096:]
        self._idle = 0.0
        if len(self._output) > OUTPUT_MEMORY_LIMIT:
            cut = len(self._output) - OUTPUT_TRIM_TO
            self._output = self._output[cut:]
            self._undelivered = max(0, self._undelivered - cut)

    def _check_exit(self) -> bool:
        try:
            # Never declare completion while payload is still buffered.
            if self.channel.recv_ready() or self.channel.recv_stderr_ready():
                return False
            if self.channel.exit_status_ready():
                self.exit_status = self.channel.recv_exit_status()
                remaining = self._terminal.flush()
                if remaining:
                    self._append(remaining)
                self.state = "completed"
                self.finished_at = time.time()
                self._close_channel()
                self._audit_final()
                return True
            if getattr(self.channel, "closed", False):
                now = time.monotonic()
                if self._closed_since is None:
                    self._closed_since = now
                    return False
                if now - self._closed_since >= self._closed_grace:
                    self._fail(
                        "Channel closed before the command reported an exit status."
                    )
                    return True
                return False
        except Exception as e:
            self._fail(f"Error checking channel status: {e}")
            return True
        return False

    def poll(self, timeout: float = 0.0) -> str:
        """Pump output for up to `timeout` seconds and return the state."""
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            if self._drain_once():
                if self._check_exit():
                    return self.state
                continue
            if self._check_exit():
                return self.state

            if self._idle >= PROMPT_DETECT_MIN_IDLE:
                kind = _classify_prompt(self._terminal.active_line)
                if kind:
                    self.state = "awaiting_input"
                    self.hint = kind
                    return self.state

            if time.monotonic() >= deadline:
                return self.state
            time.sleep(0.05)
            self._idle = round(self._idle + 0.05, 3)

    def send(self, text: str, press_enter: bool = True) -> None:
        """Write user input to the PTY (passwords are not echoed by the pty)."""
        if self.state not in ("running", "awaiting_input"):
            raise RuntimeError(
                f"Command {self.command_id} is not accepting input (state={self.state})."
            )
        payload = text + ("\n" if press_enter else "")
        self.channel.sendall(payload.encode("utf-8"))
        self.state = "running"
        self.hint = None
        self._recent = ""
        self._idle = 0.0

    def stop(self) -> None:
        """Abort the command: Ctrl+C then close the channel."""
        if self.is_terminal:
            return
        try:
            self.channel.sendall(b"\x03")
        except Exception:
            pass
        self._close_channel()
        remaining = self._terminal.flush()
        if remaining:
            self._append(remaining)
        self.state = "stopped"
        self.finished_at = time.time()
        self._audit_final()

    def _fail(self, message: str) -> None:
        self.state = "failed"
        self.error = message
        self.finished_at = time.time()
        self._close_channel()
        remaining = self._terminal.flush()
        if remaining:
            self._append(remaining)
        self._audit_final()

    def _close_channel(self) -> None:
        try:
            self.channel.close()
        except Exception:
            pass

    def _audit_final(self) -> None:
        if self._noted:
            return
        self._noted = True
        detail = f"state={self.state}"
        if self.error:
            detail += f"; {self.error}"
        self._audit_record(session_id=self.session_id, device=self.device,
                           command=self.command, exit_status=self.exit_status,
                           source="mcp", detail=detail)

    @property
    def is_terminal(self) -> bool:
        return self.state in ("completed", "failed", "stopped")

    def summary(self) -> dict:
        return {
            "command_id": self.command_id,
            "command": self.command[:120],
            "state": self.state,
            "hint": self.hint,
            "exit_status": self.exit_status,
            "elapsed": round(self.elapsed, 1),
        }


@dataclass
class ActiveConnection:
    session_id: str
    host: str
    port: int
    username: str
    connected_at: float = field(default_factory=time.time)
    command: Optional[PtyCommand] = None
    command_seq: int = 0
    history: list = field(default_factory=list)

    @property
    def target(self) -> str:
        return f"ssh:{self.host}"


class ConnectionRegistry:
    """Holds the default (single-device) SSH session for the MCP process.

    The MCP server process is long-lived, so the paramiko connection survives
    across tool calls. Codex only calls tools; connection state lives here.
    """

    def __init__(self, device_manager=DEVICE_MANAGER):
        self._dm = device_manager
        self._lock = threading.RLock()
        self._conn: Optional[ActiveConnection] = None
        self._recent_commands: list = []  # last few PtyCommand objects

    # --- Connection lifecycle ----------------------------------------------

    def connect(self, host: str, username: str = "root",
                password: Optional[str] = None, port: int = 22) -> ActiveConnection:
        """Establish (or replace) the default SSH connection."""
        with self._lock:
            session_id = f"mcp-{host}-{uuid.uuid4().hex[:8]}"
            self._dm.connect_ssh(host, username, password, port, session_id=session_id)
            conn = self._dm.get_connection(session_id)
            transport = conn.ssh_client.get_transport()
            if transport:
                transport.set_keepalive(15)
            self._replace_locked(ActiveConnection(
                session_id=session_id, host=host, port=port, username=username,
            ))
            return self._conn

    def require(self) -> ActiveConnection:
        with self._lock:
            if self._conn is None:
                raise NotConnectedError("No active connection. Call connect(host) first.")
            return self._conn

    def active(self) -> Optional[ActiveConnection]:
        with self._lock:
            return self._conn

    def disconnect(self) -> None:
        with self._lock:
            self._replace_locked(None)

    def _replace_locked(self, new_conn: Optional[ActiveConnection]) -> None:
        old = self._conn
        if old is not None:
            if old.command and not old.command.is_terminal:
                old.command.stop()
            try:
                self._dm.disconnect(session_id=old.session_id)
            except Exception:
                pass
        self._conn = new_conn

    def reconnect(self, timeout: float = 90.0, poll_interval: float = 5.0) -> bool:
        """Re-establish the same session after a reboot (same session_id)."""
        with self._lock:
            conn = self.require()
            if conn.command and not conn.command.is_terminal:
                conn.command.stop()
            conn.command = None
            ok = self._dm.reconnect(session_id=conn.session_id,
                                    timeout=timeout, poll_interval=poll_interval)
            if not ok:
                self._replace_locked(None)
                return False
            dm_conn = self._dm.get_connection(conn.session_id)
            transport = dm_conn.ssh_client.get_transport() if dm_conn.ssh_client else None
            if transport:
                transport.set_keepalive(15)
            audit.record(session_id=conn.session_id, device=conn.target,
                         command="<reconnected>", exit_status=0, source="mcp")
            return True

    # --- Command lifecycle ---------------------------------------------------

    def start_command(self, command: str) -> PtyCommand:
        """Start a command on the default connection (one at a time)."""
        with self._lock:
            conn = self.require()
            if conn.command and not conn.command.is_terminal:
                raise CommandBusyError(conn.command)
            firewall = safety_firewall_error(command)
            if firewall:
                raise SafetyFirewallError(firewall)
            dm_conn = self._dm.get_connection(conn.session_id)
            if dm_conn.conn_type != "ssh" or not dm_conn.ssh_client:
                raise ConnectionError("The default connection is not an active SSH session.")
            conn.command_seq += 1
            cmd = PtyCommand(
                dm_conn.ssh_client, command, f"c{conn.command_seq}",
                session_id=conn.session_id, device=conn.target,
            )
            conn.command = cmd
            return cmd

    def note_finished(self, cmd: PtyCommand) -> None:
        """Record a terminal command into the connection history."""
        if not cmd.is_terminal:
            return
        with self._lock:
            if cmd in self._recent_commands:
                return
            conn = self._conn
            if conn is not None:
                conn.history.append(cmd.summary())
                conn.history = conn.history[-10:]
            self._recent_commands.append(cmd)
            self._recent_commands = self._recent_commands[-5:]

    def find_command(self, command_id: str = "") -> Optional[PtyCommand]:
        """Find a command by id (default: the active one, else the latest)."""
        with self._lock:
            candidates = []
            if self._conn is not None and self._conn.command is not None:
                candidates.append(self._conn.command)
            candidates.extend(self._recent_commands)
            if not command_id:
                return candidates[0] if candidates else None
            for cmd in candidates:
                if cmd.command_id == command_id:
                    return cmd
            return None

    def upload(self, local_path: str, remote_path: str) -> str:
        conn = self.require()
        return self._dm.upload_file(local_path, remote_path, session_id=conn.session_id)

    def download(self, remote_path: str, local_path: str) -> str:
        conn = self.require()
        return self._dm.download_file(remote_path, local_path, session_id=conn.session_id)

    def status_text(self) -> str:
        with self._lock:
            if self._conn is None:
                return "Not connected. Call connect(host, username, port) first."
            c = self._conn
            uptime = int(time.time() - c.connected_at)
            lines = [
                f"Connected: {c.username}@{c.host}:{c.port}",
                f"Session: {c.session_id} (uptime {uptime}s, keepalive 15s)",
            ]
            if c.command is not None:
                cmd = c.command
                hint = f" hint={cmd.hint}" if cmd.hint else ""
                lines.append(
                    f"Active command: {cmd.command_id} [{cmd.state}{hint}] "
                    f"'{cmd.command[:80]}' ({cmd.elapsed:.0f}s)"
                )
            else:
                lines.append("Active command: none")
            if c.history:
                lines.append("Recent commands:")
                for h in reversed(c.history[-5:]):
                    lines.append(
                        f"  {h['command_id']} [{h['state']}] exit={h['exit_status']} {h['command']}"
                    )
            return "\n".join(lines)

    def shutdown(self) -> None:
        try:
            self.disconnect()
        except Exception:
            pass


REGISTRY = ConnectionRegistry()
atexit.register(REGISTRY.shutdown)
