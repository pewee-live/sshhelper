import time
import paramiko
import serial
import re
import threading
import sys
from typing import Optional
from getpass import getpass
import os
from datetime import datetime
from langchain_core.tools import tool
from langchain_core.runnables import RunnableConfig


# Match ANSI escape sequences (colors, cursor moves, clears, etc.)
_ANSI_RE = re.compile(r'\x1b\[[0-9;]*[A-Za-z]')
# Match other control sequences (OSC, single-shift, etc.)
_CTRL_RE = re.compile(r'\x1b\][^\x07]*\x07|\x1b[=>]')


class TerminalOutputFilter:
    """Statefully normalize a PTY/serial stream across read chunks.

    A stateless cleaner cannot implement terminal semantics.  A spinner sent as
    ``-\\b\\\\\\b|\\b`` becomes ``-\\\\|`` when backspaces are merely deleted,
    and a progress line split across reads can leak every animation frame.  This
    filter keeps a bounded terminal screen, applies CR/BS/cursor/erase commands,
    exposes only finalized lines to agents, and throttles live screen snapshots
    sent to the browser.
    """

    MAX_SCREEN_LINES = 5000
    MAX_ROW_CHARS = 8192
    MAX_RENDER_CHARS = 128_000
    MAX_LIVE_CHARS = 64_000
    LIVE_INTERVAL = 0.5

    _CSI_RE = re.compile(r'^\x1b\[([0-9;?]*)([A-Za-z])')
    _OSC_RE = re.compile(r'^\x1b\].*?(?:\x07|\x1b\\)', re.DOTALL)
    _PRINTABLE_RE = re.compile(r'[^\x00-\x1f\x7f]')
    _MEANINGFUL_RE = re.compile(r'[^\W_]+', re.UNICODE)

    def __init__(self):
        # Screen state is used for interactive rendering; finalized lines are a
        # stable transcript for LLM/MCP consumers.
        self._rows = [[]]
        self._row = 0
        self._col = 0
        self._pending = ""
        self._stable_lines = []
        self._stable_cursor = 0
        self._dropped_rows = 0

        self._last_live_time = 0.0
        self._last_live_fingerprint = None

    def _ensure_row(self, row: int):
        while row >= len(self._rows):
            self._rows.append([])
            if len(self._rows) > self.MAX_SCREEN_LINES:
                self._rows.pop(0)
                row -= 1
                self._row = row
                self._dropped_rows += 1

    def _put_char(self, char: str):
        self._ensure_row(self._row)
        row = self._rows[self._row]
        if self._col > self.MAX_ROW_CHARS:
            # Keep the head marker and resume at the end; extremely wide lines
            # are almost always binary/noise rather than useful diagnostics.
            self._col = self.MAX_ROW_CHARS
        while len(row) <= self._col:
            row.append(" ")
        row[self._col] = char
        self._col += 1

    def _erase_in_line(self, mode: int):
        self._ensure_row(self._row)
        row = self._rows[self._row]
        if mode == 0:
            del row[self._col:]
        elif mode == 1:
            del row[:self._col]
            self._col = 0
        else:
            row.clear()
            self._col = 0

    def _erase_in_display(self, mode: int):
        if mode == 2 or (mode == 0 and self._row == 0 and self._col == 0):
            self._rows = [[]]
            self._row = self._col = 0
            return
        self._ensure_row(self._row)
        if mode == 0:
            del self._rows[self._row][self._col:]
            del self._rows[self._row + 1:]
        elif mode == 1:
            del self._rows[:self._row]
            self._row = 0
            self._erase_in_line(2)
            self._ensure_row(0)

    def _handle_csi(self, params: str, final: str):
        try:
            raw = [int(p) for p in params.split(";") if p.isdigit()]
        except ValueError:
            raw = []
        count = raw[0] if raw else 1

        if final == "A":
            self._row = max(0, self._row - max(1, count))
        elif final in ("B", "e"):
            self._row += max(1, count)
            self._ensure_row(self._row)
        elif final in ("C", "a"):
            self._col = max(0, self._col + max(1, count))
        elif final == "D":
            self._col = max(0, self._col - max(1, count))
        elif final == "G":
            self._col = max(0, count - 1)
        elif final in ("H", "f"):
            row = max(1, raw[0] if len(raw) > 0 else 1) - 1
            col = max(1, raw[1] if len(raw) > 1 else 1) - 1
            self._row, self._col = row, col
            self._ensure_row(row)
        elif final == "J":
            self._erase_in_display(raw[0] if raw else 0)
        elif final == "K":
            self._erase_in_line(raw[0] if raw else 0)
        elif final == "L":
            for _ in range(max(1, count)):
                self._rows.insert(self._row, [])
        elif final == "M":
            for _ in range(min(max(1, count), len(self._rows) - self._row)):
                self._rows.pop(self._row)
        # SGR and less common sequences intentionally have no visible effect.

    def _finish_pending(self) -> bool:
        if not self._pending:
            return True

        osc = self._OSC_RE.match(self._pending)
        if osc:
            self._pending = self._pending[osc.end():]
            return True

        if self._pending.startswith("\x1b]"):
            # An unterminated OSC sequence must wait for its terminator.
            return False

        csi = self._CSI_RE.match(self._pending)
        if csi:
            self._handle_csi(csi.group(1), csi.group(2))
            self._pending = self._pending[csi.end():]
            return True

        if self._pending.startswith("\x1b["):
            return False

        if self._pending.startswith("\x1b"):
            if len(self._pending) >= 2:
                command = self._pending[1]
                if command == "7":
                    # DECSC: cursor position only; character attributes are ignored.
                    self._saved_cursor = (self._row, self._col)
                elif command == "8":
                    row, col = getattr(self, "_saved_cursor", (0, 0))
                    self._row, self._col = row, col
                elif command == "M":
                    self._row = max(0, self._row - 1)
                elif command == "D":
                    self._row += 1
                self._pending = self._pending[2:]
                return True
            return False
        self._pending = ""
        return True

    def _newline(self):
        self._ensure_row(self._row)
        line = "".join(self._rows[self._row]).rstrip()
        self._stable_lines.append(line)
        if len(self._stable_lines) > self.MAX_SCREEN_LINES:
            cut = len(self._stable_lines) - self.MAX_SCREEN_LINES
            self._stable_lines = self._stable_lines[cut:]
            self._stable_cursor = max(0, self._stable_cursor - cut)
        self._row += 1
        self._col = 0
        self._ensure_row(self._row)

    def push(self, raw: str):
        self._pending += raw
        pos = 0
        while pos < len(self._pending):
            # Keep partial escape sequences for the next read chunk.
            if self._pending[pos] == "\x1b":
                self._pending = self._pending[pos:]
                if not self._finish_pending():
                    return
                # A complete sequence was consumed; continue processing any
                # ordinary characters that arrived in the same read chunk.
                pos = 0
                continue

            char = self._pending[pos]
            pos += 1
            if char == "\n":
                self._newline()
            elif char == "\r":
                self._col = 0
            elif char == "\x08":
                self._col = max(0, self._col - 1)
            elif char == "\t":
                next_tab = (self._col // 8 + 1) * 8
                while self._col < next_tab:
                    self._put_char(" ")
            elif char >= " " and char != "\x7f":
                self._put_char(char)

        self._pending = self._pending[pos:]
        self._finish_pending()

    @staticmethod
    def _collapse_noise(text: str) -> str:
        """Bound control-free spinner/progress floods as a final safety net."""
        def mixed(match):
            chars = match.group(0)
            return f"[dynamic output suppressed: {len(chars)} chars]"

        # A long mixed run composed solely of common spinner/progress glyphs is
        # not useful transcript content.  Short separators remain untouched.
        text = re.sub(r'[-\\|/*+oO.#@°○◯ ]{60,}', mixed, text)

        def repeated(match):
            char = match.group(1)
            count = len(match.group(0))
            return f"{char * 8}[repeated {count} chars]"

        text = re.sub(r'(.)\1{199,}', repeated, text)
        return text

    def render(self, max_chars: int = MAX_RENDER_CHARS) -> str:
        rows = self._rows[:self._row + 1]
        text = "\n".join("".join(row).rstrip() for row in rows).rstrip("\n")
        if self._dropped_rows:
            text = f"[... {self._dropped_rows} earlier output lines dropped ...]\n" + text
        text = self._collapse_noise(text)
        if len(text) <= max_chars:
            return text
        keep = max_chars // 2
        return (
            text[:keep]
            + f"\n[... {len(text) - max_chars} middle output characters dropped ...]\n"
            + text[-keep:]
        )

    def take_stable(self) -> str:
        """Return finalized lines not yet delivered to an LLM/MCP caller."""
        if self._stable_cursor >= len(self._stable_lines):
            return ""
        chunk = "\n".join(self._stable_lines[self._stable_cursor:]) + "\n"
        self._stable_cursor = len(self._stable_lines)
        return chunk

    def flush(self):
        """Finish the active row and return any newly stable transcript text."""
        active = self.active_line
        if active or self._col:
            self._stable_lines.append(active)
        self._row += 1
        self._col = 0
        self._ensure_row(self._row)
        return self.take_stable()

    @property
    def active_line(self) -> str:
        self._ensure_row(self._row)
        return "".join(self._rows[self._row]).rstrip()

    def tail(self, lines: int = 8) -> str:
        source = self._stable_lines + ([self.active_line] if self.active_line else [])
        return "\n".join(source[-max(1, lines):])

    def poll_live(self, force: bool = False) -> str:
        """Return a bounded full-screen snapshot for a replace-style UI event."""
        now = time.monotonic()
        display = self.render(self.MAX_LIVE_CHARS)
        # Spinner-only changes have no alphanumeric fingerprint.  Suppress them
        # instead of repainting the browser at hundreds of frames per second.
        fingerprint = tuple(self._MEANINGFUL_RE.findall(display))
        changed = fingerprint != self._last_live_fingerprint
        due = force or (changed and now - self._last_live_time >= self.LIVE_INTERVAL)
        if not due or not display:
            return ""
        self._last_live_time = now
        self._last_live_fingerprint = fingerprint
        return display + "\n"


def _clean_terminal_output(text: str) -> str:
    """Strip ANSI escape codes and normalize carriage-return progress bars.

    Programs like pip, apt, wget write progress updates using '\\r' to overwrite
    the current line. Without a real terminal the raw stream concatenates every
    frame, producing a wall of junk. We keep only the last segment of each
    '\\r'-delimited line so the output shows the final state, not every frame.
    """
    cleaner = TerminalOutputFilter()
    cleaner.push(text)
    cleaner.flush()
    return cleaner.render()


import audit


# --- Safe mode command classification -----------------------------------------
# Used to intercept state-changing commands when safe mode is active, so the
# constraint is enforced at the tool layer (guaranteed) rather than only in the
# system prompt (best-effort).

# Commands/patterns that MODIFY system state -- blocked under safe mode.
_MODIFY_PATTERNS = [
    r"\b(apt|apt-get|dpkg|snap|pip|pip3|npm|yarn|gem|cargo|go install)\b.*\b(install|remove|purge|upgrade|autoremove)\b",
    r"\b(systemctl|service|rc-update|init\.d)\b.*\b(start|stop|restart|reload|enable|disable)\b",
    r"\b(echo|printf|cat|tee)\b.*>",
    r"\bsed\b.*-i",
    r"\b(dd|mkfs|fdisk|parted)\b",
    r"\b(mkdir|rmdir|rm|mv|cp|install|truncate)\b",
    r"\btouch\b",
    r"\b(chmod|chown|chattr|setfacl)\b",
    r"\biptables\b",
    r"\b(ip|ifconfig)\b.*\b(add|del|set|flush|down)\b",
    r"\bnft\b",
    r"\bufw\b",
    r"\biwconfig\b",
    r"\bnmcli\b.*\b(add|modify|delete)\b",
    r"\b(useradd|userdel|usermod|groupadd|groupdel|passwd|chpasswd|adduser|deluser)\b",
    r"\b(reboot|shutdown|poweroff|halt|init\s+[06])\b",
    r"\bcrontab\b",
    r"\bat\b",
    r"\bsystemctl\b.*\btimer\b",
    r"\bmount\b",
    r"\bumount\b",
    r"\bswap(on|off)\b",
    r"\b(modprobe|insmod|rmmod)\b",
    r"\b(mysql|psql|sqlite3|mongod|redis-cli)\b.*\b(CREATE|DROP|ALTER|INSERT|UPDATE|DELETE|TRUNCATE)\b",
]

_MODIFY_RE = [re.compile(p, re.IGNORECASE) for p in _MODIFY_PATTERNS]


def _classify_command(command: str) -> str:
    """Classify a shell command as 'read' (safe) or 'modify' (state-changing).

    Used by the safe-mode guard to block modifications at the tool layer."""
    cmd = command.strip()
    for pattern in _MODIFY_RE:
        if pattern.search(cmd):
            return "modify"
    return "read"


def _safe_mode_block_message(command: str) -> str:
    """Return a user-friendly message when safe mode blocks a command."""
    return (
        f"[SAFE MODE] The command below was BLOCKED because it modifies system state.\n"
        f"Blocked command: {command}\n\n"
        f"Safe mode is ON. The agent will NOT execute this command. Instead, review it and run it manually if you decide it is safe.\n"
        f"Toggle safe mode off in the UI if you want the agent to execute it directly."
    )


# Password / credential prompt keywords (English and Chinese).
# When the terminal's last line contains one of these, the manager immediately
# asks the human for the secret and forwards it.
PASSWORD_PROMPT_KEYWORDS = [
    "password:",
    "Password:",
    "password for",
    "Password for",
    "\u5bc6\u7801",  # "密码" (password) in Chinese
    "Enter PIN",
    "Username for",
    "username for",
    "login:",
    "Login:",
    "Username:",
    "username:",
]

# Substrings that indicate a yes/no style confirmation, a menu choice, or a
# "press enter" prompt -- the command will hang forever unless something is
# typed. Used to fast-trigger human intervention.
INTERACTIVE_PROMPT_PATTERNS = [
    "[y/n]", "[y]/n", "[yes/no]", "(yes/no)", "(y/n)", "(y/n]",
    "(yes/no/files)", "[yes/no/files]",
    "[default=", "[default:", "press enter", "hit enter", "press return",
    "do you accept", "do you want to continue", "do you want to",
    "are you sure", "proceed?", "proceed (", "continue?",
    "[o/n]", "(o/n)", "go ahead", "y or n",
]

# If a command produces no output for this many seconds and we did not recognise
# a specific prompt, we hand control to the human.
INTERVENTION_IDLE_TIMEOUT = 15.0
# After a "keep waiting" decision, do not bother the human again for this long.
INTERVENTION_COOLDOWN = 45.0
# Minimum idle seconds before reacting to a recognised prompt line, so we do not
# fire on a transient line that happens to end with '?' right before the process
# exits naturally.
PROMPT_DETECT_MIN_IDLE = 0.3

# Full-screen / interactive programs that would deadlock a non-interactive PTY.
BLOCKED_INTERACTIVE_TOOLS = {
    "htop", "vi", "vim", "nano", "ncdu", "tmux", "screen", "minicom",
}


def find_blocked_interactive_apps(command: str) -> list:
    """Return the blocked full-screen apps referenced by a shell command."""
    blocked = []
    for token in command.split():
        clean_token = re.sub(r"['\"`()&|;<>~]", '', token)
        base_name = os.path.basename(clean_token)
        if base_name in BLOCKED_INTERACTIVE_TOOLS:
            blocked.append(base_name)
    return blocked


def safety_firewall_error(command: str) -> Optional[str]:
    """Shared safety firewall for all execution paths (Web/CLI/MCP).

    Returns an error message when the command must be rejected, else None.
    """
    blocked = find_blocked_interactive_apps(command)
    if blocked:
        blocked_str = ", ".join(sorted(set(blocked)))
        return (
            f"Error: Command rejected by safety firewall. '{blocked_str}' is an "
            "interactive/full-screen program which causes terminal deadlocks. "
            "Please use non-interactive alternatives (e.g., 'cat', 'sed -i', 'top -b -n 1')."
        )
    if "sensors-detect" in command and "--auto" not in command:
        return (
            "Error: Command rejected by safety firewall. 'sensors-detect' is interactive "
            "and will wait for user input indefinitely, causing the agent to hang. "
            "Please use 'sensors-detect --auto' instead."
        )
    return None


def _cli_intervention(context, session_id=None):
    """Default (CLI) intervention handler: ask on the local terminal."""
    print("\n[Manual Intervention Required] Recent terminal output:")
    print(context or "(no output)")
    print("Type the input to send, or 'abort' to cancel, or 'wait' to keep waiting.")
    try:
        val = input("Your choice: ").strip()
    except Exception:
        return {"action": "wait"}
    low = val.lower()
    if low in ("abort", "a"):
        return {"action": "abort"}
    if low in ("wait", "w", ""):
        return {"action": "wait"}
    return {"action": "send", "input": val}


class Connection:
    """Represents a single hardware connection (SSH or Serial)."""
    def __init__(self):
        self.conn_type: Optional[str] = None
        self.ssh_client: Optional[paramiko.SSHClient] = None
        self.serial_client: Optional[serial.Serial] = None
        self.active_channel: Optional[paramiko.Channel] = None
        # Persisted connection parameters, used to re-establish the link after a
        # reboot (reconnect) and to open SFTP channels for file transfer.
        self.ssh_host: Optional[str] = None
        self.ssh_username: Optional[str] = None
        self.ssh_password: Optional[str] = None
        self.ssh_port: int = 22
        self.serial_port_name: Optional[str] = None
        self.serial_baudrate: int = 115200

    def close(self):
        if self.ssh_client:
            try:
                self.ssh_client.close()
            except Exception:
                pass
            self.ssh_client = None
        if self.serial_client:
            try:
                self.serial_client.close()
            except Exception:
                pass
            self.serial_client = None
        self.conn_type = None
        self.active_channel = None


class ConnectionManager:
    """Manages session-associated hardware connections (SSH or Serial)."""

    def __init__(self):
        # Dict of session_id -> Connection
        self.connections = {}
        # Default connection for CLI / backward compatibility
        self._default_connection = Connection()
        # Per-session locks so concurrent calls targeting the same device are
        # serialized (a device terminal can only run one command at a time).
        self._locks: dict = {}
        self._locks_guard = threading.Lock()

        # Callbacks that can be overridden by the Web server
        # `replace=True` denotes a full terminal-screen snapshot for a live UI.
        # The CLI prints only finalized chunks; the web UI consumes snapshots.
        self.on_output = lambda text, session_id=None, replace=False: (
            None if replace else print(text, end='', flush=True)
        )
        self.on_password_request = lambda prompt, session_id=None: getpass(prompt)
        self.on_state_change = lambda state, session_id=None: None
        # Human-in-the-loop intervention when a command stalls waiting for input.
        # Must return {"action": "send"|"abort"|"wait", "input": "<text>"}.
        self.on_intervention = _cli_intervention

    def get_connection(self, session_id: Optional[str] = None) -> Connection:
        if not session_id:
            return self._default_connection
        if session_id not in self.connections:
            self.connections[session_id] = Connection()
        return self.connections[session_id]

    def _get_lock(self, session_id: Optional[str]) -> threading.Lock:
        """Get or create a per-session lock for serializing device operations."""
        key = session_id or "_default"
        with self._locks_guard:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    @property
    def conn_type(self) -> Optional[str]:
        return self._default_connection.conn_type

    @conn_type.setter
    def conn_type(self, value: Optional[str]):
        self._default_connection.conn_type = value

    @property
    def ssh_client(self) -> Optional[paramiko.SSHClient]:
        return self._default_connection.ssh_client

    @ssh_client.setter
    def ssh_client(self, value: Optional[paramiko.SSHClient]):
        self._default_connection.ssh_client = value

    @property
    def serial_client(self) -> Optional[serial.Serial]:
        return self._default_connection.serial_client

    @serial_client.setter
    def serial_client(self, value: Optional[serial.Serial]):
        self._default_connection.serial_client = value

    @property
    def active_channel(self) -> Optional[paramiko.Channel]:
        return self._default_connection.active_channel

    @active_channel.setter
    def active_channel(self, value: Optional[paramiko.Channel]):
        self._default_connection.active_channel = value

    def connect_ssh(self, host: str, username: str, password: Optional[str] = None, port: int = 22, session_id: Optional[str] = None):
        conn = self.get_connection(session_id)
        conn.conn_type = "ssh"
        conn.ssh_host, conn.ssh_username, conn.ssh_password, conn.ssh_port = host, username, password, port
        conn.ssh_client = paramiko.SSHClient()
        conn.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        conn.ssh_client.connect(hostname=host, port=port, username=username, password=password, timeout=10)
        # Keep long-lived MCP/agent sessions alive across NAT/idle timeouts.
        transport = conn.ssh_client.get_transport()
        if transport:
            transport.set_keepalive(15)
        # Persist credentials into the encrypted vault so reconnect (after a
        # reboot) and the audit trail can work without a live plaintext copy.
        try:
            from vault import VAULT
            VAULT.store(
                device_key=host,
                conn_type="ssh",
                params={"host": host, "username": username, "port": port},
                secret=password,
            )
        except Exception as e:
            print(f"[vault] failed to store credential: {e}", file=sys.stderr)
        audit.record(session_id=session_id, device=f"ssh:{host}", command=f"<connect {username}@{host}:{port}>", exit_status=0, source="ui")
        return f"Successfully connected to SSH at {host}:{port}"

    def connect_serial(self, port: str, baudrate: int = 115200, session_id: Optional[str] = None):
        conn = self.get_connection(session_id)
        conn.conn_type = "serial"
        conn.serial_port_name, conn.serial_baudrate = port, baudrate
        conn.serial_client = serial.Serial(port=port, baudrate=baudrate, timeout=3)
        return f"Successfully connected to Serial port {port} at {baudrate} baud."

    def disconnect(self, session_id: Optional[str] = None):
        self.interrupt(session_id)
        conn = self.get_connection(session_id)
        conn.close()
        return "Disconnected successfully."

    def interrupt(self, session_id: Optional[str] = None):
        """Interrupts the currently running command on SSH or Serial by sending Ctrl+C (\\x03)."""
        conn = self.get_connection(session_id)
        if getattr(conn, 'active_channel', None):
            try:
                conn.active_channel.sendall(b'\x03')
                conn.active_channel.close()
            except Exception as e:
                print(f"Error during SSH command interrupt: {e}", file=sys.stderr)
            finally:
                conn.active_channel = None

        if conn.conn_type == "serial" and conn.serial_client:
            try:
                conn.serial_client.write(b'\x03')
            except Exception as e:
                print(f"Error during Serial command interrupt: {e}", file=sys.stderr)

    def reconnect(self, session_id: Optional[str] = None, timeout: float = 120.0, poll_interval: float = 5.0) -> bool:
        """Attempt to re-establish a connection to the same device using the
        stored parameters. Used after a reboot/restart so the agent can keep
        working once the host comes back. Returns True once connected."""
        conn = self.get_connection(session_id)
        deadline = time.time() + timeout
        last_err = None
        # Drop the old (now dead) client objects first so we don't reuse them.
        conn.close()
        while time.time() < deadline:
            try:
                if conn.conn_type == "ssh" or conn.ssh_host:
                    conn.conn_type = "ssh"
                    conn.ssh_client = paramiko.SSHClient()
                    conn.ssh_client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
                    # Resolve the password from the encrypted vault if it isn't
                    # held in memory (e.g. after a server restart).
                    pwd = conn.ssh_password
                    if pwd is None and conn.ssh_host:
                        try:
                            from vault import VAULT
                            pwd = VAULT.resolve(conn.ssh_host)
                        except Exception:
                            pwd = None
                    conn.ssh_client.connect(
                        hostname=conn.ssh_host, port=conn.ssh_port,
                        username=conn.ssh_username, password=pwd,
                        timeout=8,
                    )
                    return True
                elif conn.conn_type == "serial" and conn.serial_port_name:
                    conn.serial_client = serial.Serial(
                        port=conn.serial_port_name, baudrate=conn.serial_baudrate, timeout=3
                    )
                    return True
                else:
                    return False
            except Exception as e:
                last_err = e
                time.sleep(poll_interval)
        print(f"[reconnect] gave up after {timeout}s: {last_err}", file=sys.stderr)
        return False

    def upload_file(self, local_path: str, remote_path: str, session_id: Optional[str] = None) -> str:
        """Upload a local file to the connected device via SFTP (SSH only), serialized per session."""
        with self._get_lock(session_id):
            return self._upload_file_impl(local_path, remote_path, session_id)

    def _upload_file_impl(self, local_path: str, remote_path: str, session_id: Optional[str] = None) -> str:
        conn = self.get_connection(session_id)
        if conn.conn_type != "ssh" or not conn.ssh_client:
            return "Error: File upload is only supported over SSH connections. Serial connections cannot transfer files."
        if not os.path.isfile(local_path):
            return f"Error: Local file not found: {local_path}"
        try:
            self.on_output(f"\n--- Uploading {os.path.basename(local_path)} -> {remote_path} ---\n", session_id)
            transport = conn.ssh_client.get_transport()
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                return "Error: Could not open SFTP channel over the SSH transport."
            sftp.put(local_path, remote_path)
            # Preserve executable bit if the source was executable.
            try:
                st = os.stat(local_path)
                sftp.chmod(remote_path, st.st_mode & 0o777)
            except Exception:
                pass
            sftp.close()
            self.on_output("--- Upload Finished ---\n", session_id)
            return f"Uploaded {local_path} -> {remote_path} ({os.path.getsize(local_path)} bytes)"
        except Exception as e:
            return f"Error uploading file: {e}"

    def download_file(self, remote_path: str, local_path: str, session_id: Optional[str] = None) -> str:
        """Download a file from the connected device, serialized per session."""
        with self._get_lock(session_id):
            return self._download_file_impl(remote_path, local_path, session_id)

    def _download_file_impl(self, remote_path: str, local_path: str, session_id: Optional[str] = None) -> str:
        conn = self.get_connection(session_id)
        if conn.conn_type != "ssh" or not conn.ssh_client:
            return "Error: File download is only supported over SSH connections. Serial connections cannot transfer files."
        try:
            os.makedirs(os.path.dirname(os.path.abspath(local_path)), exist_ok=True)
            self.on_output(f"\n--- Downloading {remote_path} -> {local_path} ---\n", session_id)
            transport = conn.ssh_client.get_transport()
            sftp = paramiko.SFTPClient.from_transport(transport)
            if sftp is None:
                return "Error: Could not open SFTP channel over the SSH transport."
            sftp.get(remote_path, local_path)
            sftp.close()
            self.on_output("--- Download Finished ---\n", session_id)
            return f"Downloaded {remote_path} -> {local_path} ({os.path.getsize(local_path)} bytes)"
        except Exception as e:
            return f"Error downloading file: {e}"

    def _log_command(self, command: str, session_id: Optional[str] = None):
        target = self.get_device_target(session_id)
        # Append-only audit record (source of truth for /api/audit).
        audit.record(session_id=session_id, device=target, command=command, source="agent")
        try:
            os.makedirs("data", exist_ok=True)
            with open("data/command_history.log", "a", encoding="utf-8") as f:
                timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                f.write(f"[{timestamp}] [{target}] {command}\n")
        except Exception as e:
            print(f"Failed to log command: {e}", file=sys.stderr)

    def get_device_key(self, session_id: Optional[str] = None) -> Optional[str]:
        """Return a stable identifier for the currently-connected device, used to
        key persistent device profiles. SSH -> host IP, Serial -> port name."""
        conn = self.get_connection(session_id)
        if conn.conn_type == "ssh" and conn.ssh_client:
            try:
                return conn.ssh_client.get_transport().getpeername()[0]
            except Exception:
                return None
        elif conn.conn_type == "serial" and conn.serial_client:
            return conn.serial_client.port
        return None

    def get_device_target(self, session_id: Optional[str] = None) -> str:
        key = self.get_device_key(session_id)
        conn = self.get_connection(session_id)
        return f"{conn.conn_type or 'Unknown'}:{key}" if key else "Unknown"

    # --- Human-in-the-loop intervention helpers -------------------------------

    @staticmethod
    def _last_line(buffer: str) -> str:
        return buffer.split('\n')[-1].strip()

    @staticmethod
    def _looks_like_prompt(line: str) -> bool:
        if not line:
            return False
        low = line.lower()
        if line.endswith("?") and len(line) <= 80:
            return True
        if line.endswith(":") and len(line) <= 60:
            return True
        for pat in INTERACTIVE_PROMPT_PATTERNS:
            if pat.lower() in low:
                return True
        return False

    def _request_intervention(self, context: str, session_id: Optional[str]) -> dict:
        try:
            result = self.on_intervention(context, session_id)
        except Exception:
            return {"action": "wait"}
        if isinstance(result, str):
            return {"action": "send", "input": result}
        if isinstance(result, dict):
            result.setdefault("action", "wait")
            result.setdefault("input", "")
            return result
        return {"action": "wait"}

    # --- Command execution ----------------------------------------------------

    def execute(self, command: str, session_id: Optional[str] = None) -> str:
        """Execute a command on the connected device, serialized per session."""
        with self._get_lock(session_id):
            return self._execute_impl(command, session_id)

    def _execute_impl(self, command: str, session_id: Optional[str] = None) -> str:
        # --- Safety Firewall (shared with the MCP server) ---
        firewall_error = safety_firewall_error(command)
        if firewall_error:
            return firewall_error

        self._log_command(command, session_id)
        self.on_state_change("Executing...", session_id)

        conn = self.get_connection(session_id)

        try:
            if conn.conn_type == "ssh" and conn.ssh_client:
                # get_pty=True allows commands like sudo to prompt for a password interactively
                stdin, stdout, stderr = conn.ssh_client.exec_command(command, get_pty=True)
                channel = stdout.channel
                conn.active_channel = channel

                cleaner = TerminalOutputFilter()
                idle_seconds = 0.0
                last_intervention = 0.0

                self.on_output(f"\n--- Executing SSH Command: {command} ---\n", session_id)

                while True:
                    got_data = False

                    if channel.recv_ready():
                        chunk_bytes = channel.recv(1024)
                        if chunk_bytes:
                            cleaner.push(chunk_bytes.decode('utf-8', errors='replace'))
                            got_data = True
                            idle_seconds = 0.0

                    if channel.recv_stderr_ready():
                        chunk_bytes = channel.recv_stderr(1024)
                        if chunk_bytes:
                            cleaner.push(chunk_bytes.decode('utf-8', errors='replace'))
                            got_data = True
                            idle_seconds = 0.0

                    if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
                        break

                    if got_data:
                        continue

                    # Snapshots are fingerprint-throttled: spinner-only changes
                    # produce no browser event, while real text is limited to 2/s.
                    display = cleaner.poll_live()
                    if display:
                        self.on_output(display, session_id, replace=True)

                    last_line = cleaner.active_line

                    # Known password prompt -> ask for credentials
                    if any(kw in last_line for kw in PASSWORD_PROMPT_KEYWORDS):
                        pwd = self.on_password_request(
                            "\n[Agent] Remote system is asking for a password/username. Please enter it: ",
                            session_id,
                        )
                        channel.sendall((pwd + "\n").encode("utf-8"))
                        idle_seconds = 0.0
                        continue

                    # Known interactive pager stuck at (END)
                    if "(END)" in last_line:
                        channel.sendall(b"q\n")
                        idle_seconds = 0.0
                        self.on_output("\n[Agent] Detected interactive pager, automatically sending 'q' to exit...\n", session_id)
                        continue

                    # Recognised yes/no style prompt -> fast human intervention
                    if idle_seconds >= PROMPT_DETECT_MIN_IDLE and self._looks_like_prompt(last_line):
                        decision = self._request_intervention(last_line, session_id)
                        if decision["action"] == "abort":
                            try:
                                channel.sendall(b"\x03")
                            except Exception:
                                pass
                            self.on_output("\n[Agent] Command aborted by user.\n", session_id)
                            break
                        elif decision["action"] == "send":
                            channel.sendall((decision.get("input", "") + "\n").encode("utf-8"))
                            self.on_output(f"\n[Agent] Sent user input: {decision.get('input', '')!r}\n", session_id)
                        idle_seconds = 0.0
                        last_intervention = time.time()
                        continue

                    # Generic stall fallback -> human intervention
                    if idle_seconds >= INTERVENTION_IDLE_TIMEOUT and (time.time() - last_intervention) >= INTERVENTION_COOLDOWN:
                        context = cleaner.tail(8) or "(no output yet)"
                        decision = self._request_intervention(context, session_id)
                        if decision["action"] == "abort":
                            try:
                                channel.sendall(b"\x03")
                            except Exception:
                                pass
                            self.on_output("\n[Agent] Command aborted by user.\n", session_id)
                            break
                        elif decision["action"] == "send":
                            channel.sendall((decision.get("input", "") + "\n").encode("utf-8"))
                            self.on_output(f"\n[Agent] Sent user input: {decision.get('input', '')!r}\n", session_id)
                        idle_seconds = 0.0
                        last_intervention = time.time()
                        continue

                    time.sleep(0.1)
                    idle_seconds += 0.1

                exit_status = channel.recv_exit_status()
                cleaner.flush()
                # The stable lines are already bounded. Use render() for the
                # final result so cursor/erase commands are reflected correctly.
                output = cleaner.render()
                display = cleaner.poll_live(force=True)
                if display:
                    self.on_output(display, session_id, replace=True)
                self.on_output("\n--- Command Finished ---\n", session_id)

                # Record exit status into the audit log (companion to _log_command).
                audit.record(
                    session_id=session_id, device=self.get_device_target(session_id),
                    command=command, exit_status=exit_status, source="agent",
                )

                result = f"Exit Status: {exit_status}\n"
                if output:
                    result += f"OUTPUT:\n{output}\n"
                if not output.strip():
                    result += "Command executed successfully, but produced no output."
                return result

            elif conn.conn_type == "serial" and conn.serial_client:
                # Clear buffer before sending
                conn.serial_client.reset_input_buffer()
                cmd_bytes = f"{command}\r\n".encode('utf-8')
                conn.serial_client.write(cmd_bytes)

                self.on_output(f"\n--- Executing Serial Command: {command} ---\n", session_id)
                cleaner = TerminalOutputFilter()
                idle_time = 0.0
                last_intervention = 0.0
                SERIAL_DONE_TIMEOUT = 2.0

                while True:
                    if conn.serial_client.in_waiting > 0:
                        idle_time = 0.0
                        try:
                            cleaner.push(conn.serial_client.read(conn.serial_client.in_waiting).decode('utf-8', errors='replace'))
                        except Exception as e:
                            err_msg = f"\n[Error reading partial output: {e}]\n"
                            self.on_output(err_msg, session_id)
                        continue

                    display = cleaner.poll_live()
                    if display:
                        self.on_output(display, session_id, replace=True)

                    last_line = cleaner.active_line

                    if any(kw in last_line for kw in PASSWORD_PROMPT_KEYWORDS):
                        pwd = self.on_password_request(
                            "\n[Agent] Serial device is asking for a password/username. Please enter it: ",
                            session_id,
                        )
                        conn.serial_client.write(f"{pwd}\r\n".encode("utf-8"))
                        idle_time = 0.0
                        continue

                    if "(END)" in last_line:
                        conn.serial_client.write(b"q\r\n")
                        idle_time = 0.0
                        self.on_output("\n[Agent] Detected interactive pager, automatically sending 'q' to exit...\n", session_id)
                        continue

                    if idle_time >= PROMPT_DETECT_MIN_IDLE and self._looks_like_prompt(last_line):
                        decision = self._request_intervention(last_line, session_id)
                        if decision["action"] == "abort":
                            try:
                                conn.serial_client.write(b"\x03")
                            except Exception:
                                pass
                            self.on_output("\n[Agent] Command aborted by user.\n", session_id)
                            break
                        elif decision["action"] == "send":
                            conn.serial_client.write((decision.get("input", "") + "\r\n").encode("utf-8"))
                            self.on_output(f"\n[Agent] Sent user input: {decision.get('input', '')!r}\n", session_id)
                        idle_time = 0.0
                        last_intervention = time.time()
                        continue

                    if idle_time >= SERIAL_DONE_TIMEOUT:
                        break

                    time.sleep(0.1)
                    idle_time += 0.1

                self.on_output("\n--- Command Finished ---\n", session_id)
                cleaner.flush()
                output = cleaner.render()
                return f"OUTPUT:\n{output}\n" if output.strip() else "Command executed successfully, but produced no output."
            else:
                return "Error: No active connection. Please ensure the agent is connected first."
        finally:
            conn.active_channel = None

    def close(self):
        self._default_connection.close()
        for conn in self.connections.values():
            conn.close()
        self.connections.clear()


# Global connection manager instance for the tools to use
DEVICE_MANAGER = ConnectionManager()


# --- Device profile memory -------------------------------------------------

import json
import re


class DeviceProfileManager:
    """Persists a per-device knowledge profile so the agent can skip re-probing
    a board it has already diagnosed. Keyed by device (host IP for SSH, port for
    Serial). Stored as JSON files under data/devices/."""

    PROFILE_FIELDS = [
        "hostname", "os", "kernel", "architecture", "cpu", "memory",
        "storage", "network", "notes",
    ]

    def __init__(self, data_dir="data/devices"):
        self.data_dir = data_dir
        os.makedirs(self.data_dir, exist_ok=True)

    @staticmethod
    def _safe_name(key: str) -> str:
        return re.sub(r"[^A-Za-z0-9._-]", "_", key or "unknown")

    def _path(self, key: str) -> str:
        return os.path.join(self.data_dir, self._safe_name(key) + ".json")

    def get_profile(self, device_key: str) -> Optional[dict]:
        if not device_key:
            return None
        path = self._path(device_key)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return json.load(f)
            except Exception:
                return None
        return None

    def get_profile_for_session(self, session_id: Optional[str]) -> Optional[dict]:
        key = DEVICE_MANAGER.get_device_key(session_id)
        return self.get_profile(key)

    def get_profile_text(self, device_key: str) -> str:
        """Render the profile as compact text for the system prompt."""
        profile = self.get_profile(device_key)
        if not profile:
            return ""
        lines = [f"DEVICE MEMORY ({device_key}):"]
        for field in self.PROFILE_FIELDS:
            val = profile.get(field)
            if val:
                lines.append(f"- {field}: {val}")
        return "\n".join(lines)

    def update(self, device_key: str, **fields) -> Optional[dict]:
        """Merge non-empty fields into the stored profile, then persist it."""
        if not device_key:
            return None
        profile = self.get_profile(device_key) or {"device_key": device_key}
        changed = False
        for field in self.PROFILE_FIELDS:
            val = fields.get(field)
            if val:
                profile[field] = val
                changed = True
        if changed:
            profile["device_key"] = device_key
            profile["updated_at"] = datetime.now().isoformat()
            path = self._path(device_key)
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(profile, f, ensure_ascii=False, indent=2)
            except Exception as e:
                print(f"Failed to save device profile: {e}")
        return profile

    def update_for_session(self, session_id: Optional[str], **fields) -> Optional[dict]:
        key = DEVICE_MANAGER.get_device_key(session_id)
        return self.update(key, **fields)

    def list_profiles(self):
        out = []
        if os.path.isdir(self.data_dir):
            for fn in os.listdir(self.data_dir):
                if fn.endswith(".json"):
                    try:
                        with open(os.path.join(self.data_dir, fn), "r", encoding="utf-8") as f:
                            out.append(json.load(f))
                    except Exception:
                        pass
        return out


# Global device profile manager
DEVICE_PROFILE_MANAGER = DeviceProfileManager()


@tool
def execute_device_command(command: str, config: RunnableConfig) -> str:
    """
    Executes a shell or terminal command on the connected hardware device (via SSH or Serial).
    Use this tool to run commands to diagnose issues, check logs, or configure the device.

    Args:
        command: The shell command to run (e.g., 'dmesg', 'lsmod', 'cat /var/log/syslog').

    Returns:
        The standard output and standard error from the command execution.
    """
    print(f"\n[TOOL EXECUTING COMMAND]: {command}")
    session_id = config.get("configurable", {}).get("session_id")
    try:
        # Safe mode guard: block state-changing commands at the tool layer.
        # The system prompt asks the agent not to run them, but this is a
        # guaranteed backstop in case the LLM doesn't comply.
        safe_mode = config.get("configurable", {}).get("safe_mode", False)
        if safe_mode and _classify_command(command) == "modify":
            return _safe_mode_block_message(command)
        return DEVICE_MANAGER.execute(command, session_id=session_id)
    except Exception as e:
        return f"Error executing command: {str(e)}"


@tool
def save_device_profile(
    config: RunnableConfig,
    hostname: str = "",
    os_info: str = "",
    kernel: str = "",
    architecture: str = "",
    cpu: str = "",
    memory: str = "",
    storage: str = "",
    network: str = "",
    notes: str = "",
) -> str:
    """
    Save or update this device's profile to long-term memory, so future debugging
    sessions on the SAME device can skip re-running basic probes (uname, lscpu,
    free, etc.). Call this once you have gathered the device's basic identity.
    Only the fields you provide are updated; others are left unchanged.

    Args:
        hostname: Device hostname (e.g. 'rock-2f').
        os_info: Operating system and version (e.g. 'Debian GNU/Linux 12 (bookworm)').
        kernel: Kernel version (e.g. '6.1.43-26-rk2312').
        architecture: CPU architecture (e.g. 'aarch64', 'x86_64').
        cpu: CPU model and core count (e.g. 'ARM Cortex-A53 x4').
        memory: Total memory (e.g. '1.9GiB').
        storage: Notable storage (e.g. '29G eMMC (mmcblk1), 61% used').
        network: Key network interfaces/IPs (e.g. 'enp1s0 192.168.0.108').
        notes: Any other durable facts worth remembering for next time.
    """
    session_id = config.get("configurable", {}).get("session_id")
    profile = DEVICE_PROFILE_MANAGER.update_for_session(
        session_id,
        hostname=hostname,
        os=os_info,
        kernel=kernel,
        architecture=architecture,
        cpu=cpu,
        memory=memory,
        storage=storage,
        network=network,
        notes=notes,
    )
    if profile:
        return f"Device profile saved/updated. Current profile: {profile}"
    return "Error: Could not determine the connected device to save a profile for. Ensure a connection is active."


@tool
def upload_file(local_path: str, remote_path: str, config: RunnableConfig) -> str:
    """
    Upload a local file to the connected device via SFTP (SSH connections only).
    Use this to push firmware images, config files, or scripts onto the device.
    The executable bit of the local file is preserved on the remote side.

    Args:
        local_path: Path to the file on the server running the agent (not the device).
        remote_path: Absolute path on the device where the file should be written.
    """
    session_id = config.get("configurable", {}).get("session_id")
    return DEVICE_MANAGER.upload_file(local_path, remote_path, session_id=session_id)


@tool
def download_file(remote_path: str, local_path: str, config: RunnableConfig) -> str:
    """
    Download a file from the connected device to the server running the agent
    via SFTP (SSH connections only). Use this to pull logs, configs, or dumps
    back for local inspection.

    Args:
        remote_path: Absolute path on the device to fetch.
        local_path: Path on the server running the agent where the file is saved.
    """
    session_id = config.get("configurable", {}).get("session_id")
    return DEVICE_MANAGER.download_file(remote_path, local_path, session_id=session_id)


@tool
def reboot_and_wait(config: RunnableConfig, wait_seconds: int = 60) -> str:
    """
    Reboot the connected device and wait for it to come back online, re-establishing
    the connection automatically. Use this instead of a raw 'reboot' command,
    because a raw reboot would kill the session and leave the agent unable to
    continue. Only SSH connections can be auto-reconnected (the stored credentials
    are reused); serial devices are rebooted but cannot be reliably re-detected.

    Args:
        wait_seconds: Maximum seconds to wait for the device to come back online
            before giving up. Default 60. Increase for slow-booting boards.
    """
    session_id = config.get("configurable", {}).get("session_id")
    conn = DEVICE_MANAGER.get_connection(session_id)
    if not conn.conn_type or (conn.conn_type == "ssh" and not conn.ssh_host):
        return "Error: No active connection to reboot."
    is_ssh = conn.conn_type == "ssh"

    # Fire the reboot command without waiting for its (impossible) response.
    try:
        if is_ssh:
            # nohup + & so the channel closing doesn't abort the reboot itself.
            conn.ssh_client.exec_command("nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &", timeout=5)
        else:
            conn.serial_client.write(b"reboot\r\n")
        DEVICE_MANAGER.on_output("\n--- Reboot issued. Waiting for device to come back... ---\n", session_id)
    except Exception as e:
        # The command often raises as the connection drops; that is expected.
        DEVICE_MANAGER.on_output(f"\n[reboot] connection dropped as expected: {e}\n", session_id)

    if not is_ssh:
        # Serial devices need manual reconnection; we can't auto-detect boot completion.
        try:
            conn.close()
        except Exception:
            pass
        return "Reboot issued on serial device. The serial link dropped; reconnect manually once the board finishes booting, then continue."

    # SSH: poll and reconnect using stored credentials.
    DEVICE_MANAGER.on_state_change("Reconnecting...", session_id)
    ok = DEVICE_MANAGER.reconnect(session_id=session_id, timeout=wait_seconds, poll_interval=5.0)
    DEVICE_MANAGER.on_state_change("Executing...", session_id)
    if ok:
        # Quick sanity probe to confirm the host is actually responsive.
        try:
            result = DEVICE_MANAGER.execute("echo alive; uptime", session_id=session_id)
            return f"Device rebooted and reconnected successfully. Host is back online.\n{result}"
        except Exception as e:
            return f"Device reconnected but the sanity probe failed: {e}"
    return f"Error: Device did not come back online within {wait_seconds} seconds. Check it manually."

# ---------------------------------------------------------------------------
# Industrial protocol tools: SNMP, Modbus, Redfish, IPMI.
# These query devices that have NO shell at all -- network gear, PLCs, BMCs.
# ---------------------------------------------------------------------------



def _resolve_industrial_params(config, kind, kwargs):
    """Fill in missing host/credentials from the session's connection_params,
    so the user doesn't have to repeat them after configuring a connection in the UI."""
    params = (config or {}).get("configurable", {}).get("connection_params", {}) or {}
    if not kwargs.get("host") and params.get("host"):
        kwargs["host"] = params["host"]
    if kind == "snmp":
        if kwargs.get("community", "public") == "public" and params.get("community"):
            kwargs["community"] = params["community"]
        if kwargs.get("port", 161) == 161 and params.get("port"):
            kwargs["port"] = int(params["port"])
    elif kind in ("redfish", "ipmi"):
        if not kwargs.get("username") and params.get("username"):
            kwargs["username"] = params["username"]
        if not kwargs.get("password") and params.get("password"):
            kwargs["password"] = params["password"]
        if kwargs.get("port", 0) in (0, 443, 623) and params.get("port"):
            kwargs["port"] = int(params["port"])
    elif kind == "modbus":
        if kwargs.get("port", 502) == 502 and params.get("port"):
            kwargs["port"] = int(params["port"])
        if kwargs.get("unit_id", 1) == 1 and params.get("unit_id"):
            kwargs["unit_id"] = int(params["unit_id"])
    return kwargs

from industrial import SnmpClient, ModbusClient, RedfishClient, IpmiClient

# Per-session industrial clients (lazily created, reused across calls).
_industrial_clients = {}  # session_id -> {"snmp":..., "modbus":..., ...}


def _get_industrial(session_id, kind, factory):
    """Get-or-create an industrial client of `kind` for a session."""
    bucket = _industrial_clients.setdefault(session_id, {})
    if kind not in bucket:
        bucket[kind] = factory()
    return bucket[kind]


@tool
def snmp_query(host: str = "", oid_or_name: str = "", operation: str = "get",
               community: str = "public", port: int = 161, version: int = 2,
               config: RunnableConfig = None) -> str:
    """
    Query a network device (switch, router, PDU, UPS, AP) via SNMP. Use this for
    devices that have no shell -- they expose status only through SNMP OIDs.

    Args:
        host: IP/hostname of the SNMP-enabled device.
        oid_or_name: An OID (e.g. '1.3.6.1.2.1.1.1.0') or a known name like
            'sysDescr', 'sysUpTime', 'ifNumber', 'ifOperStatus', 'ifInOctets'.
        operation: 'get' for a single scalar value, or 'walk' for a table subtree.
        community: SNMP community string (default 'public').
        port: SNMP UDP port (default 161).
        version: 1 or 2 (default 2 = SNMPv2c).
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    p = _resolve_industrial_params(config, "snmp", {"host": host, "community": community, "port": port, "version": version})
    if not p["host"]:
        return "Error: No host specified. Configure an SNMP connection first, or pass the host argument."
    try:
        client = _get_industrial(session_id, "snmp", lambda: SnmpClient(p["host"], p["community"], p["port"], p["version"]))
        if operation == "walk":
            return client.walk(oid_or_name)
        return client.get(oid_or_name)
    except Exception as e:
        return f"Error: SNMP query failed: {e}"


@tool
def modbus_query(host: str = "", operation: str = "", address: int = 0, value=None,
                 count: int = 1, port: int = 502, unit_id: int = 1,
                 config: RunnableConfig = None) -> str:
    """
    Read or write a Modbus TCP device (PLC, sensor, energy meter, drive).
    Use this for industrial equipment that speaks Modbus and has no shell.

    Args:
        host: IP/hostname of the Modbus TCP device.
        operation: One of 'read_holding_registers', 'read_coils', 'write_register', 'write_coil'.
        address: Starting register/coil address (0-based).
        value: Required for write operations (register value as int, or 0/1 for coil).
        count: Number of registers/coils to read (default 1, ignored for writes).
        port: Modbus TCP port (default 502).
        unit_id: Modbus slave/unit ID (default 1).
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    try:
        client = _get_industrial(session_id, "modbus", lambda: ModbusClient(host, port, unit_id))
        if operation == "read_holding_registers":
            return client.read_holding_registers(address, count)
        elif operation == "read_coils":
            return client.read_coils(address, count)
        elif operation == "write_register":
            if value is None:
                return "Error: write_register requires a 'value' argument."
            return client.write_register(address, value)
        elif operation == "write_coil":
            return client.write_coil(address, bool(value))
        else:
            return f"Error: Unknown modbus operation '{operation}'. Use read_holding_registers, read_coils, write_register, or write_coil."
    except Exception as e:
        return f"Error: Modbus operation failed: {e}"


@tool
def redfish_query(host: str = "", username: str = "", password: str = "", path: str = "",
                  port: int = 443, use_https: bool = True,
                  config: RunnableConfig = None) -> str:
    """
    Query a server BMC via the Redfish REST API (DMTF). Use this for out-of-band
    server management -- power state, sensors, firmware, inventory, event logs.
    Common paths: 'Systems', 'Chassis', 'Managers', 'Chassis/1/Thermal',
    'Systems/1'. Leave path empty to get the service root for discovery.

    Args:
        host: IP/hostname of the BMC.
        username: BMC username (e.g. 'root', 'admin').
        password: BMC password.
        path: Redfish resource path relative to /redfish/v1/ (or a full /redfish/ path).
        port: BMC HTTPS port (default 443, sometimes 443 on iLO / 5900 on others).
        use_https: Use HTTPS (default true). Set false for insecure/dev BMCs.
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    try:
        client = _get_industrial(
            session_id, "redfish",
            lambda: RedfishClient(host, username, password, port, use_https),
        )
        if not path:
            return client.root()
        return client.get(path)
    except Exception as e:
        return f"Error: Redfish query failed: {e}"


@tool
def ipmi_query(host: str = "", username: str = "", password: str = "", operation: str = "power",
               port: int = 623, config: RunnableConfig = None) -> str:
    """
    Query a server BMC via IPMI 2.0 (RMCP+ LAN). Use this for older servers whose
    BMC does not support Redfish, or when Redfish is not available.

    Args:
        host: IP/hostname of the BMC.
        username: BMC username.
        password: BMC password.
        operation: One of 'power' (power state), 'sensors' (temperature/voltage/fan
            readings), 'sel' (system event log), 'inventory' (hardware identity).
        port: IPMI RMCP+ port (default 623).
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    try:
        client = _get_industrial(
            session_id, "ipmi",
            lambda: IpmiClient(host, username, password, port),
        )
        if operation == "power":
            return client.get_power_state()
        elif operation == "sensors":
            return client.get_sensors()
        elif operation == "sel":
            return client.get_sel()
        elif operation == "inventory":
            return client.get_identify()
        else:
            return f"Error: Unknown ipmi operation '{operation}'. Use power, sensors, sel, or inventory."
    except Exception as e:
        return f"Error: IPMI query failed: {e}"


# ---------------------------------------------------------------------------
# Batch orchestration: run a command across a group of devices concurrently.
# ---------------------------------------------------------------------------

from device_groups import GROUP_MANAGER
from concurrent.futures import ThreadPoolExecutor, as_completed
import traceback


def _run_on_one_device(spec: dict, command: str) -> dict:
    """Open a throwaway SSH connection, run a command, return the result.
    Does NOT use the session-bound ConnectionManager -- batch ops are independent
    so they can fan out without colliding with an active session's connection."""
    host = spec.get("host")
    result = {"host": host, "status": "error", "output": "", "exit_status": None}
    if not host:
        result["output"] = "No host specified."
        return result
    if spec.get("conn_type", "ssh") != "ssh":
        result["output"] = f"Batch execution only supports SSH; {host} is {spec.get('conn_type')}."
        return result
    password = spec.get("password")
    # If no stored secret, try the username/password passed inline.
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(
            hostname=host,
            port=spec.get("port", 22),
            username=spec.get("username", "root"),
            password=password,
            timeout=10,
        )
        try:
            stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=120)
            output = stdout.read().decode("utf-8", errors="replace")
            exit_status = stdout.channel.recv_exit_status()
            err = stderr.read().decode("utf-8", errors="replace")
            if err:
                output += "\n[stderr]\n" + err
            result["status"] = "success" if exit_status == 0 else "failed"
            result["exit_status"] = exit_status
            result["output"] = output.strip()[:2000]  # cap per-device output
        finally:
            client.close()
    except Exception as e:
        result["output"] = f"Connection error: {e}"
    return result


@tool
def batch_run(group_id: str, command: str, batch_size: int = 10,
              max_failure_pct: int = 20, config: RunnableConfig = None) -> str:
    """
    Run a shell command across ALL devices in a device group, concurrently. Use
    this for fleet-wide operations like kernel upgrades, config changes, or bulk
    diagnostics. Results from every device are collected and returned.

    Rolling/batch mode: devices are processed in waves of `batch_size`. If the
    failure rate exceeds `max_failure_pct`, execution HALTS to prevent a bad
    change from spreading to the remaining devices (fail-fast protection).

    Args:
        group_id: The device group ID (from list_device_groups). The group must
            exist and its devices must have SSH credentials stored in the vault.
        command: The shell command to run on every device.
        batch_size: How many devices to run concurrently in one wave (default 10).
        max_failure_pct: Stop the whole rollout if more than this percent of
            devices in a wave fail (default 20). Set to 100 to disable fail-fast.
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    # Safe mode guard: batch operations are always state-changing.
    safe_mode = (config or {}).get("configurable", {}).get("safe_mode", False)
    if safe_mode:
        return _safe_mode_block_message(f"batch_run on group {group_id}: {command}")
    group = GROUP_MANAGER.get(group_id)
    if not group:
        return f"Error: Device group '{group_id}' not found. Use list_device_groups to see available groups."

    specs = GROUP_MANAGER.resolve_credentials(group)
    # Filter to devices that actually have resolvable credentials.
    ready = [s for s in specs if s.get("username")]
    missing = [s["host"] for s in specs if not s.get("username")]
    if not ready:
        return f"Error: No devices in group '{group.name}' have resolvable SSH credentials. Store credentials first via the vault."

    # Audit the batch operation.
    audit.record(session_id=session_id, device=f"batch:{group.name}",
                 command=f"<batch_run {command[:80]} on {len(ready)} devices>", source="agent")

    all_results = []
    aborted = False
    total_done = 0

    # Process in waves of batch_size.
    for wave_start in range(0, len(ready), batch_size):
        wave = ready[wave_start:wave_start + batch_size]
        wave_results = []
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_run_on_one_device, spec, command): spec for spec in wave}
            for fut in as_completed(futures):
                try:
                    res = fut.result()
                except Exception as e:
                    res = {"host": futures[fut].get("host"), "status": "error",
                           "output": f"Exception: {e}", "exit_status": None}
                wave_results.append(res)

        all_results.extend(wave_results)
        total_done += len(wave_results)

        # Fail-fast: check failure rate within this wave.
        failures = sum(1 for r in wave_results if r["status"] != "success")
        failure_rate = (failures / len(wave_results) * 100) if wave_results else 0
        if failure_rate > max_failure_pct and wave_start + batch_size < len(ready):
            aborted = True
            remaining = len(ready) - total_done
            break

    # Build the summary report.
    succeeded = sum(1 for r in all_results if r["status"] == "success")
    failed = sum(1 for r in all_results if r["status"] != "success")
    lines = [f"BATCH OPERATION REPORT: '{command[:60]}'"]
    lines.append(f"Group: {group.name} | Devices attempted: {len(all_results)}/{len(ready)}")
    lines.append(f"Result: {succeeded} succeeded, {failed} failed")
    if missing:
        lines.append(f"Skipped (no credentials): {', '.join(missing)}")
    if aborted:
        lines.append(f"ABORTED: failure rate exceeded {max_failure_pct}% in a wave. {remaining} devices NOT touched.")
    lines.append("")
    for r in all_results:
        status_icon = "OK" if r["status"] == "success" else "FAIL"
        lines.append(f"[{status_icon}] {r['host']} (exit {r['exit_status']})")
        if r["status"] != "success" or "error" in r.get("output", "").lower():
            lines.append(f"     {r['output'][:200]}")
    return "\n".join(lines)


@tool
def list_device_groups(config: RunnableConfig = None) -> str:
    """
    List all saved device groups with their IDs, names, and device counts.
    Use this before batch_run to find the right group_id.
    """
    groups = GROUP_MANAGER.list()
    if not groups:
        return "No device groups defined yet. Create one via the web UI or POST /api/device-groups."
    lines = ["Device Groups:"]
    for g in groups:
        lines.append(f"  - {g['group_id']}: '{g['name']}' ({g['device_count']} devices)")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Configuration baseline & drift detection
# ---------------------------------------------------------------------------

from baseline import BASELINE_MANAGER, DEFAULT_PROBES


@tool
def snapshot_config(config: RunnableConfig = None,
                    probes: str = "") -> str:
    """
    Capture a configuration snapshot of the connected device right now. This
    records ip addr, iptables, routes, mounts, running services, and key config
    files so that future changes can be detected by diffing against this baseline.

    Use this when you want to establish a known-good baseline BEFORE making
    changes, or to capture the current state for later comparison. Call
    diff_config afterwards to see what changed.

    Args:
        probes: Optional comma-separated list of specific probes to run instead
            of the default set (e.g. 'ip_addr,iptables'). Leave empty for all.
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    conn = DEVICE_MANAGER.get_connection(session_id)
    if not conn.conn_type:
        return "Error: No active connection. Connect to a device first."

    device_key = DEVICE_MANAGER.get_device_key(session_id)
    if not device_key:
        return "Error: Could not determine device key for snapshot."

    # Decide which probes to run.
    if probes.strip():
        requested = [p.strip() for p in probes.split(",")]
        probe_map = {k: v for k, v in DEFAULT_PROBES.items() if k in requested}
        if not probe_map:
            return f"Error: Unknown probe names. Available: {', '.join(DEFAULT_PROBES.keys())}"
    else:
        probe_map = DEFAULT_PROBES

    DEVICE_MANAGER.on_output(f"\n--- Capturing config snapshot ({len(probe_map)} probes)... ---\n", session_id)

    results = {}
    for name, cmd in probe_map.items():
        try:
            DEVICE_MANAGER.on_output(f"  probing {name}...", session_id)
            output = DEVICE_MANAGER.execute(cmd, session_id=session_id)
            # Strip the wrapper lines added by execute() for cleaner storage.
            lines = output.split("\n")
            cleaned = []
            capture = False
            for line in lines:
                if line.startswith("OUTPUT:"):
                    capture = True
                    continue
                if line.startswith("--- Command Finished ---") or line.startswith("Exit Status:"):
                    capture = False
                    continue
                if capture:
                    cleaned.append(line)
            results[name] = "\n".join(cleaned).strip()[:5000]
        except Exception as e:
            results[name] = f"<error: {e}>"

    snapshot = BASELINE_MANAGER.save_snapshot(device_key, results)
    audit.record(
        session_id=session_id, device=f"ssh:{device_key}",
        command=f"<snapshot_config {len(results)} probes>", exit_status=0, source="agent",
    )
    return (f"Snapshot saved for {device_key} at {snapshot['datetime']}.\n"
            f"Captured {len(results)} config areas: {', '.join(results.keys())}.\n"
            f"Use diff_config later to see what changed since this snapshot.")


@tool
def diff_config(config: RunnableConfig = None,
                older_timestamp: str = "",
                newer_timestamp: str = "") -> str:
    """
    Compare two configuration snapshots of the connected device to detect drift.
    By default, compares the latest snapshot against the one before it. Use this
    to answer "what changed on this device?" -- e.g. after a network outage, to
    see if iptables or routing was modified.

    Args:
        older_timestamp: Timestamp of the older snapshot to compare from (YYYYMMDD_HHMMSS).
            Leave empty to auto-use the second-newest snapshot.
        newer_timestamp: Timestamp of the newer snapshot to compare to. Leave empty
            to auto-use the newest snapshot.
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    device_key = DEVICE_MANAGER.get_device_key(session_id)
    if not device_key:
        return "Error: Could not determine device key."

    snaps = BASELINE_MANAGER.list_snapshots(device_key)
    if len(snaps) < 2:
        return (f"Only {len(snaps)} snapshot(s) exist for {device_key}. "
                "Need at least 2 to diff. Call snapshot_config again after making changes.")

    diff_result = BASELINE_MANAGER.diff(
        device_key,
        newer_ts=newer_timestamp or None,
        older_ts=older_timestamp or None,
    )
    if not diff_result:
        return "Error: Could not compute diff (snapshots may be missing)."

    return BASELINE_MANAGER.format_diff(diff_result)


@tool
def list_snapshots(config: RunnableConfig = None) -> str:
    """
    List all configuration snapshots for the connected device with timestamps
    and which config areas were captured. Use this before diff_config to pick
    specific timestamps, or just to see the snapshot history.
    """
    session_id = (config or {}).get("configurable", {}).get("session_id")
    device_key = DEVICE_MANAGER.get_device_key(session_id)
    if not device_key:
        return "Error: Could not determine device key."

    snaps = BASELINE_MANAGER.list_snapshots(device_key)
    if not snaps:
        return f"No snapshots exist for {device_key}. Call snapshot_config to capture one."
    lines = [f"Snapshots for {device_key}:"]
    for s in snaps:
        lines.append(f"  {s['timestamp']} ({s['datetime']}) -- {s['probe_count']} probes: {', '.join(s['probe_names'][:5])}")
    return "\n".join(lines)
