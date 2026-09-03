"""MCP Server -- exposes device-operation tools via the Model Context Protocol.

This module wraps the system's core capabilities as MCP tools so that any
MCP-compatible AI agent (Claude Desktop, Cursor, Cline, etc.) can discover and
call them directly -- no HTTP, no OpenAPI spec parsing required.

Architecture: persistent SSH state lives in mcp_connections.ConnectionRegistry
(the MCP server process is long-lived, so one `connect()` can serve many
`run()` calls). Stateless tools call the same underlying modules (VAULT,
GROUP_MANAGER, BASELINE_MANAGER) as the web UI and external_api.

Run standalone:
    python mcp_server.py          # stdio transport (for Claude Desktop, etc.)
    python mcp_server.py --http   # HTTP transport (for remote agents)

Or import the server object:
    from mcp_server import mcp
"""
import os
import sys
import re

# Ensure the project root is on the path when run as a standalone script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
# All runtime data (vault, audit, baselines) is stored relative to the project
# root; pin it so the server works regardless of the client's working dir.
os.chdir(os.path.dirname(os.path.abspath(__file__)))
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

from fastmcp import FastMCP
from typing import Optional

from tools import (
    DEVICE_MANAGER,
    DEVICE_PROFILE_MANAGER,
    _clean_terminal_output,
    _run_on_one_device,
    safety_firewall_error,
)
from mcp_connections import (
    REGISTRY,
    CommandBusyError,
    NotConnectedError,
    SafetyFirewallError,
)
from vault import VAULT
from device_groups import GROUP_MANAGER
from baseline import BASELINE_MANAGER, DEFAULT_PROBES
import audit
import paramiko

mcp = FastMCP("Hardware Debugging Tools")

# stdio MCP uses stdout for the protocol stream: silence the console-oriented
# callbacks on the shared DeviceManager (the MCP PTY runner does not use them).
DEVICE_MANAGER.on_output = lambda text, session_id=None: None
DEVICE_MANAGER.on_state_change = lambda state, session_id=None: None
DEVICE_MANAGER.on_password_request = lambda prompt, session_id=None: None
DEVICE_MANAGER.on_intervention = lambda context, session_id=None: {"action": "abort"}


# ---------------------------------------------------------------------------
# Shared helper (same logic as external_api._resolve_credentials / _exec_ssh_one_shot,
# kept here to avoid a cross-module dependency from MCP -> external_api).
# ---------------------------------------------------------------------------

def _resolve_password(host: str, password: Optional[str] = None) -> Optional[str]:
    """Use the provided password, or fall back to the encrypted vault."""
    if password:
        return password
    return VAULT.resolve(host)


def _exec_ssh(host: str, command: str, username: str = "root",
              password: Optional[str] = None, port: int = 22) -> str:
    """Open a temporary SSH connection, run a command, return cleaned output."""
    pwd = _resolve_password(host, password)
    try:
        client = paramiko.SSHClient()
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
        client.connect(hostname=host, port=port, username=username, password=pwd, timeout=10)
        try:
            stdin, stdout, stderr = client.exec_command(command, get_pty=True, timeout=120)
            output = _clean_terminal_output(stdout.read().decode("utf-8", errors="replace"))
            exit_status = stdout.channel.recv_exit_status()
            err = _clean_terminal_output(stderr.read().decode("utf-8", errors="replace"))
            if err:
                output += "\n[stderr]\n" + err
            client.close()
            return f"Exit Status: {exit_status}\n{_clip(output.strip())}"
        except Exception as e:
            client.close()
            return f"Error: {e}"
    except Exception as e:
        return f"Connection error: {e}"


# ---------------------------------------------------------------------------
# Persistent-session helpers
# ---------------------------------------------------------------------------

MAX_TOOL_OUTPUT = 8000
RUN_TIMEOUT_CAP = 600


def _clip(text: str, limit: int = MAX_TOOL_OUTPUT) -> str:
    """Keep head + tail of oversized output so exit lines survive."""
    text = text or ""
    if len(text) <= limit:
        return text
    keep = limit // 2
    return (
        text[:keep]
        + "\n...[output truncated]...\n"
        + text[-(limit - keep - 26):]
    )


def _format_result(cmd, body: str = None) -> str:
    """Render a PtyCommand state for the model in a compact, uniform way."""
    header = f"[{cmd.command_id} {cmd.state}"
    if cmd.hint:
        header += f" hint={cmd.hint}"
    if cmd.exit_status is not None:
        header += f" exit={cmd.exit_status}"
    header += "]"

    if body is None:
        body = cmd.output.strip() or "(no output)"
    body = body.strip() or "(no new output)"

    if cmd.state == "completed":
        return f"{header}\nOUTPUT:\n{_clip(body)}"
    if cmd.state == "failed":
        err = f"\nError: {cmd.error}" if cmd.error else ""
        return f"{header}{err}\nRECENT OUTPUT:\n{_clip(cmd.tail(80))}"
    if cmd.state == "stopped":
        return f"{header}\nLAST OUTPUT:\n{_clip(cmd.tail(80))}"
    if cmd.state == "awaiting_input":
        guidance = (
            "Ask the user for the password and call send_input(text). "
            "The PTY will not echo it."
            if cmd.hint == "password"
            else "Ask the user how to answer, then call send_input(text) (e.g. 'y')."
        )
        return (
            f"{header}\nRECENT OUTPUT:\n{_clip(cmd.tail(20))}\n"
            f"NEXT: {guidance}"
        )
    # running
    return (
        f"{header}\nCOMMAND: {cmd.command}\n"
        f"PARTIAL OUTPUT:\n{_clip(cmd.tail(120))}\n"
        f"NEXT: call get_output(\"{cmd.command_id}\", wait_seconds=...) to keep "
        "watching, or stop_command() to abort."
    )


def _run_simple(command: str, timeout: float = 20.0):
    """Run one command to completion on the persistent session (helper path)."""
    cmd = REGISTRY.start_command(command)
    cmd.poll(timeout)
    if not cmd.is_terminal:
        cmd.stop()
    REGISTRY.note_finished(cmd)
    return cmd


# ---------------------------------------------------------------------------
# MCP Tools
# ---------------------------------------------------------------------------

@mcp.tool()
def execute_command(host: str, command: str, username: str = "root",
                    password: str = "", port: int = 22) -> str:
    """Execute a shell command on a remote device via SSH.

    Args:
        host: IP address or hostname of the target device.
        command: The shell command to run (e.g. 'uname -a', 'df -h').
        username: SSH username (default 'root').
        password: SSH password. Leave empty to use stored vault credentials.
        port: SSH port (default 22).

    Returns:
        The command's exit status and output.
    """
    blocked = safety_firewall_error(command)
    audit.record(
        session_id=None,
        device=f"ssh:{host}",
        command=command[:200],
        source="mcp",
        detail="blocked by safety firewall" if blocked else None,
    )
    if blocked:
        return blocked
    return _exec_ssh(host, command, username, password or None, port)


# ---------------------------------------------------------------------------
# Persistent-session tools (Codex workflow: connect once, run many)
# ---------------------------------------------------------------------------

@mcp.tool()
def connect(host: str, username: str = "root", password: str = "",
            port: int = 22, verify: bool = True) -> str:
    """Connect to a device over SSH and keep the connection alive for later calls.

    This establishes the persistent default session used by run(), send_input(),
    get_output(), upload_file(), download_file(), reboot_device() and
    snapshot_config(). Call it once per device, then execute many commands.

    Args:
        host: IP address or hostname of the target device.
        username: SSH username (default 'root').
        password: SSH password. Leave empty to use the encrypted vault or key auth.
        port: SSH port (default 22).
        verify: After connecting, run 'uname -smr; hostname' once to verify the
            exec channel works (default true).

    Returns:
        Connection info plus a quick device identity probe, or an error.
    """
    try:
        conn = REGISTRY.connect(host, username, _resolve_password(host, password or None), port)
    except Exception as e:
        return f"Error connecting to {host}:{port}: {e}"

    lines = [
        f"Connected: {username}@{host}:{port}",
        f"Session: {conn.session_id} (persistent, keepalive 15s)",
    ]
    if verify:
        try:
            cmd = _run_simple("uname -smr; hostname", timeout=10)
        except Exception as e:
            REGISTRY.disconnect()
            return f"Error: connected but verification failed: {e}"
        if cmd.state == "completed" and cmd.exit_status == 0:
            lines.append(f"Verify: exit=0\n{cmd.output.strip()}")
        else:
            result = _format_result(cmd)
            if cmd.state == "completed":
                REGISTRY.disconnect()
                return f"Error: verification command failed on the device.\n{result}"
            lines.append(f"Verify: inconclusive (channel {cmd.state}). Connection kept.")
    lines.append("Next: call run(command) as many times as you like; disconnect() when done.")
    return "\n".join(lines)


@mcp.tool()
def run(command: str, timeout: int = 30) -> str:
    """Run a shell command on the connected device (persistent SSH session).

    Connect with connect() first. Commands execute one at a time on a PTY.

    Returns the exit status and output when the command finishes. If it is
    still running after `timeout` seconds, returns partial output plus a
    command_id you can poll with get_output(). If sudo or apt asks for input,
    returns state awaiting_input -- ask the user, then use send_input().

    Args:
        command: Shell command to execute (e.g. 'uname -a', 'dmesg | tail -50').
        timeout: Seconds to wait for completion, 1-600 (default 30). Long-running
            commands are NOT killed on timeout; poll them with get_output().

    Returns:
        '[<id> <state> exit=<n>] OUTPUT: ...' plus next-step guidance.
    """
    timeout = max(1, min(int(timeout), RUN_TIMEOUT_CAP))
    try:
        cmd = REGISTRY.start_command(command)
    except NotConnectedError as e:
        return f"Error: {e}"
    except CommandBusyError as e:
        active = e.active
        return (
            f"Error: {e}\n{_format_result(active)}"
        )
    except SafetyFirewallError as e:
        return str(e)
    except Exception as e:
        return f"Error starting command: {e}"

    cmd.poll(timeout)
    if cmd.is_terminal:
        REGISTRY.note_finished(cmd)
    cmd.take_new_output()  # run() already reported the full output
    return _format_result(cmd)


@mcp.tool()
def send_input(text: str, press_enter: bool = True, wait_seconds: int = 10) -> str:
    """Send user input to the interactive command currently waiting on the device.

    Use this when run() returned state 'awaiting_input' (sudo password,
    [y/n] confirmation, menu choice...). For passwords the PTY does not echo
    the text. Ask the human user first; do not guess secrets.

    Args:
        text: The exact input to send (password, 'y', a number, ...).
        press_enter: Append a newline (default true).
        wait_seconds: Seconds to watch for new output after sending (default 10).

    Returns:
        New output produced after the input, with the command state.
    """
    cmd = REGISTRY.find_command("")
    if cmd is None:
        return "Error: No command has been started yet. Call run(command) first."
    if not (cmd.state in ("running", "awaiting_input")):
        return f"Error: command {cmd.command_id} is {cmd.state} and cannot accept input."
    try:
        cmd.send(text, press_enter)
    except Exception as e:
        return f"Error sending input: {e}"
    cmd.poll(max(0, min(int(wait_seconds), RUN_TIMEOUT_CAP)))
    if cmd.is_terminal:
        REGISTRY.note_finished(cmd)
    new_output = cmd.take_new_output()
    prefix = f"[{cmd.command_id} after send_input]\nNEW OUTPUT:\n"
    return prefix + (_clip(new_output) if new_output.strip() else "(none yet)")


@mcp.tool()
def get_output(command_id: str = "", wait_seconds: int = 0, tail_lines: int = 120) -> str:
    """Fetch new output from a command started by run() (poll long-running ones).

    Args:
        command_id: The id returned by run() (e.g. 'c2'). Empty means the
            active command, or the most recent one.
        wait_seconds: Extra seconds to wait for output/completion (default 0).
        tail_lines: When providing full context for a still-running command,
            how many trailing lines to include (default 120).

    Returns:
        Only the output that arrived since the last get_output()/send_input()
        call, plus the current state (and exit status once completed).
    """
    cmd = REGISTRY.find_command(command_id)
    if cmd is None:
        return f"Error: Unknown command_id '{command_id}'. Call run() first."
    if not cmd.is_terminal:
        cmd.poll(max(0, min(int(wait_seconds), RUN_TIMEOUT_CAP)))
    if cmd.is_terminal:
        REGISTRY.note_finished(cmd)
    new_output = cmd.take_new_output()
    header = f"[{cmd.command_id} {cmd.state}"
    if cmd.hint:
        header += f" hint={cmd.hint}"
    if cmd.exit_status is not None:
        header += f" exit={cmd.exit_status}"
    header += "]"
    if new_output.strip():
        return f"{header}\nNEW OUTPUT:\n{_clip(new_output)}"
    if cmd.is_terminal:
        return f"{header}\n(no new output)"
    return (
        f"{header}\n(no new output yet; still running)\n"
        f"RECENT TAIL:\n{_clip(cmd.tail(tail_lines))}"
    )


@mcp.tool()
def stop_command(command_id: str = "") -> str:
    """Abort a running or interactive command (Ctrl+C, then close the channel).

    Args:
        command_id: The id returned by run(). Empty means the active command.

    Returns:
        The final state and the last output lines.
    """
    cmd = REGISTRY.find_command(command_id)
    if cmd is None:
        return f"Error: Unknown command_id '{command_id}'."
    if cmd.is_terminal:
        return f"{cmd.command_id} already {cmd.state}."
    cmd.stop()
    REGISTRY.note_finished(cmd)
    return _format_result(cmd)


@mcp.tool()
def status() -> str:
    """Show the persistent connection state: device, session, active command, history."""
    return REGISTRY.status_text()


@mcp.tool()
def disconnect() -> str:
    """Disconnect the persistent device session and stop any active command."""
    REGISTRY.disconnect()
    return "Disconnected."


@mcp.tool()
def snmp_query(host: str, oid_or_name: str, operation: str = "get",
               community: str = "public", port: int = 161) -> str:
    """Query a network device (switch, router, PDU, UPS) via SNMP.

    Args:
        host: Device IP address.
        oid_or_name: An OID (e.g. '1.3.6.1.2.1.1.1.0') or a known name like
            'sysDescr', 'sysUpTime', 'ifNumber', 'ifOperStatus', 'ifInOctets'.
        operation: 'get' for a single value, 'walk' for a table subtree.
        community: SNMP community string (default 'public').
        port: SNMP UDP port (default 161).

    Returns:
        The queried SNMP value(s).
    """
    from industrial import SnmpClient
    client = SnmpClient(host, community, port)
    if operation == "walk":
        return client.walk(oid_or_name)
    return client.get(oid_or_name)


@mcp.tool()
def modbus_query(host: str, operation: str, address: int, value: int = 0,
                 count: int = 1, port: int = 502, unit_id: int = 1) -> str:
    """Read or write a Modbus TCP device (PLC, sensor, energy meter).

    Args:
        host: Device IP address.
        operation: One of 'read_holding_registers', 'read_coils',
            'write_register', 'write_coil'.
        address: Starting register/coil address (0-based).
        value: Value for write operations (register int, or 0/1 for coil).
        count: Number of registers/coils to read (default 1).
        port: Modbus TCP port (default 502).
        unit_id: Modbus slave/unit ID (default 1).

    Returns:
        The read values or write confirmation.
    """
    from industrial import ModbusClient
    client = ModbusClient(host, port, unit_id)
    try:
        if operation == "read_holding_registers":
            return client.read_holding_registers(address, count)
        elif operation == "read_coils":
            return client.read_coils(address, count)
        elif operation == "write_register":
            return client.write_register(address, value)
        elif operation == "write_coil":
            return client.write_coil(address, bool(value))
        else:
            return f"Error: Unknown operation '{operation}'"
    finally:
        client.close()


@mcp.tool()
def redfish_query(host: str, username: str, password: str, path: str = "",
                  port: int = 443, use_https: bool = True) -> str:
    """Query a server BMC via the Redfish REST API (DMTF).

    Args:
        host: BMC IP address.
        username: BMC username.
        password: BMC password.
        path: Redfish resource path (e.g. 'Systems', 'Chassis/1/Thermal').
            Leave empty to get the service root for discovery.
        port: BMC HTTPS port (default 443).
        use_https: Use HTTPS (default true).

    Returns:
        The Redfish resource as JSON text.
    """
    from industrial import RedfishClient
    client = RedfishClient(host, username, password, port, use_https)
    try:
        result = client.root() if not path else client.get(path)
        return result
    finally:
        client.close()


@mcp.tool()
def ipmi_query(host: str, username: str, password: str, operation: str = "power",
               port: int = 623) -> str:
    """Query a server BMC via IPMI 2.0 (RMCP+ LAN).

    Args:
        host: BMC IP address.
        username: BMC username.
        password: BMC password.
        operation: 'power' (power state), 'sensors' (temperature/voltage/fan),
            'sel' (system event log), or 'inventory' (hardware identity).
        port: IPMI RMCP+ port (default 623).

    Returns:
        The queried IPMI data as text.
    """
    from industrial import IpmiClient
    client = IpmiClient(host, username, password, port)
    ops = {
        "power": client.get_power_state,
        "sensors": client.get_sensors,
        "sel": client.get_sel,
        "inventory": client.get_identify,
    }
    if operation not in ops:
        return f"Error: Unknown operation '{operation}'. Use: power, sensors, sel, inventory."
    return ops[operation]()


@mcp.tool()
def upload_file(local_path: str, remote_path: str) -> str:
    """Upload a file to the connected device via SFTP (persistent session).

    Args:
        local_path: Path to the file on this server.
        remote_path: Destination path on the device.

    Returns:
        Upload confirmation with file size.
    """
    if not os.path.isfile(local_path):
        return f"Error: Local file not found: {local_path}"
    try:
        return REGISTRY.upload(local_path, remote_path)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def download_file(remote_path: str, local_path: str) -> str:
    """Download a file from the connected device via SFTP (persistent session).

    Args:
        remote_path: Path on the device to fetch.
        local_path: Where to save the file on this server.

    Returns:
        Download confirmation with file size.
    """
    try:
        return REGISTRY.download(remote_path, local_path)
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def reboot_device(wait_seconds: int = 90) -> str:
    """Reboot the connected device, wait for it to return, then verify.

    Runs on the persistent session: it issues the reboot, waits for SSH to
    come back (reconnecting the same session), then runs 'echo alive; uptime'.

    Args:
        wait_seconds: Max seconds to wait for SSH to return (default 90).

    Returns:
        Confirmation that the device rebooted and reconnected, or a timeout error.
    """
    try:
        conn_info = REGISTRY.require()
        dm_conn = DEVICE_MANAGER.get_connection(conn_info.session_id)
        try:
            dm_conn.ssh_client.exec_command(
                "nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &", timeout=5
            )
        except Exception:
            pass  # The link may drop immediately; reconnect() handles it.
        audit.record(session_id=conn_info.session_id, device=conn_info.target,
                     command="<reboot>", source="mcp")
    except Exception as e:
        return f"Error: {e}"

    ok = REGISTRY.reconnect(timeout=wait_seconds, poll_interval=5.0)
    if not ok:
        return (
            f"Error: Device did not come back within {wait_seconds}s. "
            "The session was cleared; call connect() to retry."
        )
    try:
        cmd = _run_simple("echo alive; uptime", timeout=15)
    except Exception as e:
        return f"Reconnected, but the liveness probe failed: {e}"
    return f"Device rebooted and reconnected.\n{_format_result(cmd)[:500]}"


@mcp.tool()
def batch_run(group_id: str, command: str, batch_size: int = 10,
              max_failure_pct: int = 20) -> str:
    """Run a shell command across ALL devices in a device group, concurrently.

    Devices are processed in waves of batch_size. If the failure rate in a wave
    exceeds max_failure_pct, execution halts to prevent spreading a bad change.
    Credentials are resolved from the encrypted vault.

    Args:
        group_id: Device group ID (use list_device_groups to find it).
        command: Shell command to run on every device.
        batch_size: Concurrent devices per wave (default 10).
        max_failure_pct: Abort if failure rate exceeds this percent (default 20).

    Returns:
        A summary report with per-device success/failure status.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed
    group = GROUP_MANAGER.get(group_id)
    if not group:
        return f"Error: Device group '{group_id}' not found."
    specs = GROUP_MANAGER.resolve_credentials(group)
    ready = [s for s in specs if s.get("username")]
    if not ready:
        return "Error: No devices with resolvable credentials."

    audit.record(session_id=None, device=f"batch:{group.name}",
                 command=f"<mcp batch {command[:80]} on {len(ready)} devices>", source="mcp")

    all_results = []
    aborted = False
    for wave_start in range(0, len(ready), batch_size):
        wave = ready[wave_start:wave_start + batch_size]
        wave_results = []
        with ThreadPoolExecutor(max_workers=len(wave)) as pool:
            futures = {pool.submit(_run_on_one_device, spec, command): spec for spec in wave}
            for fut in as_completed(futures):
                wave_results.append(fut.result())
        all_results.extend(wave_results)
        failures = sum(1 for r in wave_results if r["status"] != "success")
        if (failures / len(wave_results) * 100) > max_failure_pct and wave_start + batch_size < len(ready):
            aborted = True
            break

    succeeded = sum(1 for r in all_results if r["status"] == "success")
    failed = len(all_results) - succeeded
    lines = [f"BATCH: {succeeded} ok, {failed} failed, aborted={aborted}"]
    for r in all_results:
        icon = "OK" if r["status"] == "success" else "FAIL"
        lines.append(f"  [{icon}] {r['host']} (exit {r.get('exit_status')})")
        if r["status"] != "success":
            lines.append(f"       {r.get('output', '')[:200]}")
    return "\n".join(lines)


@mcp.tool()
def list_device_groups() -> str:
    """List all saved device groups with their IDs, names, and device counts."""
    groups = GROUP_MANAGER.list()
    if not groups:
        return "No device groups defined."
    lines = ["Device Groups:"]
    for g in groups:
        lines.append(f"  {g['group_id']}: '{g['name']}' ({g['device_count']} devices)")
    return "\n".join(lines)


@mcp.tool()
def snapshot_config() -> str:
    """Capture a configuration baseline snapshot of the connected device.

    Records ip addr, iptables, routes, mounts, running services, SSH config, etc.
    into a timestamped snapshot for later drift comparison.

    Returns:
        Confirmation with the snapshot timestamp and probe count.
    """
    try:
        conn_info = REGISTRY.require()
        probes = {}
        for name, probe_cmd in DEFAULT_PROBES.items():
            try:
                c = _run_simple(probe_cmd, timeout=20)
                probes[name] = (
                    c.output.strip()[:5000]
                    if c.state == "completed"
                    else f"<{c.state}>"
                )
            except Exception as e:
                probes[name] = f"<error: {e}>"
        snapshot = BASELINE_MANAGER.save_snapshot(conn_info.host, probes)
        return (
            f"Snapshot saved at {snapshot['timestamp']} with {len(probes)} probes "
            f"for {conn_info.host} (persistent session)."
        )
    except Exception as e:
        return f"Error: {e}"


@mcp.tool()
def diff_config(host: str, older_timestamp: str = "", newer_timestamp: str = "") -> str:
    """Compare two configuration snapshots of a device to detect drift.

    Defaults to comparing the latest snapshot against the one before it.

    Args:
        host: Device IP address.
        older_timestamp: Older snapshot timestamp (empty = auto second-newest).
        newer_timestamp: Newer snapshot timestamp (empty = auto newest).

    Returns:
        A drift report showing which config areas changed (added/removed lines).
    """
    snaps = BASELINE_MANAGER.list_snapshots(host)
    if len(snaps) < 2:
        return f"Only {len(snaps)} snapshot(s) for {host}. Need at least 2 to diff."
    diff = BASELINE_MANAGER.diff(host, newer_ts=newer_timestamp or None, older_ts=older_timestamp or None)
    if not diff:
        return "Error: Could not compute diff."
    return BASELINE_MANAGER.format_diff(diff)


@mcp.tool()
def list_snapshots(host: str) -> str:
    """List configuration-baseline snapshots saved for a device.

    Args:
        host: Device IP address or identifier used when snapshots were saved.

    Returns:
        Snapshot timestamps, dates, probe counts, and probe names, newest first.
    """
    snapshots = BASELINE_MANAGER.list_snapshots(host)
    if not snapshots:
        return f"No snapshots found for '{host}'. Call snapshot_config() first."
    lines = [f"Found {len(snapshots)} snapshot(s) for '{host}':"]
    for snap in snapshots:
        lines.append(
            f"  {snap['timestamp']}  {snap.get('datetime', '-')}  "
            f"{snap.get('probe_count', 0)} probes: {', '.join(snap.get('probe_names', []))}"
        )
    return "\n".join(lines)


@mcp.tool()
def get_snapshot(host: str, timestamp: str, probe: str = "") -> str:
    """Read one saved configuration-baseline snapshot.

    Args:
        host: Device IP address or identifier used when the snapshot was saved.
        timestamp: Snapshot timestamp from list_snapshots().
        probe: Optional probe name (e.g. 'ip_addr'). Empty returns all probes.

    Returns:
        Snapshot metadata and captured probe output.
    """
    snapshot = BASELINE_MANAGER.get_snapshot(host, timestamp)
    if not snapshot:
        return f"Snapshot '{timestamp}' not found for '{host}'."
    probes = snapshot.get("probes", {})
    if probe:
        if probe not in probes:
            return f"Probe '{probe}' not found in snapshot '{timestamp}'."
        probes = {probe: probes[probe]}
    lines = [
        f"Snapshot: {snapshot.get('timestamp', timestamp)}",
        f"Device: {snapshot.get('device_key', host)}",
        f"Saved: {snapshot.get('datetime', '-')}",
    ]
    for name, value in probes.items():
        lines.extend([f"\n=== {name} ===", value or "(empty)"])
    return _clip("\n".join(lines))


@mcp.tool()
def search_kb(query: str) -> str:
    """Search the knowledge base for relevant troubleshooting cases.

    Searches case titles, tags, search_queries, and full text, returning the
    best matches ranked by relevance.

    Args:
        query: Natural-language search query (e.g. 'ping not working', 'docker install').

    Returns:
        Matching cases with titles, domains, and relevance scores.
    """
    import case_generator
    cases = case_generator.list_cases()
    q_lower = query.lower().strip()
    query_tokens = {
        token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", q_lower)
        if len(token) > 1
    }
    scored = []
    for c in cases:
        score = 0
        title = c.get("title", "").lower()
        title_tokens = {
            token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", title)
            if len(token) > 1
        }
        if q_lower in title:
            score += 10
        score += min(8, len(query_tokens & title_tokens) * 4)
        for t in c.get("tags", []):
            t_lower = str(t).lower()
            tag_tokens = {
                token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", t_lower)
                if len(token) > 1
            }
            if q_lower in t_lower or t_lower in q_lower:
                score += 5
            score += min(4, len(query_tokens & tag_tokens) * 2)
        for sq in c.get("search_queries", []):
            sq_lower = str(sq).lower()
            if q_lower in sq_lower or sq_lower in q_lower:
                score += 3
            sq_tokens = {
                token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", sq_lower)
                if len(token) > 1
            }
            score += min(3, len(query_tokens & sq_tokens))
        try:
            with open(c["path"], "r", encoding="utf-8") as f:
                content = f.read().lower()
                content_tokens = {
                    token for token in re.split(r"[^0-9a-z\u4e00-\u9fff]+", content)
                    if len(token) > 1
                }
                if q_lower in content:
                    score += 1
                score += min(2, len(query_tokens & content_tokens))
        except Exception:
            pass
        if score > 0:
            scored.append((score, c))
    scored.sort(key=lambda x: x[0], reverse=True)
    if not scored:
        return f"No cases matched '{query}'."
    lines = [f"Found {len(scored)} matching case(s) for '{query}':"]
    for score, c in scored[:10]:
        lines.append(f"  [score {score}] {c['domain']}/{c['title']}")
    return "\n".join(lines)


@mcp.tool()
def get_device_profile(device_key: str) -> str:
    """Retrieve the stored profile (memory) for a specific device.

    Args:
        device_key: Device IP address or identifier.

    Returns:
        The device's profile: hostname, OS, kernel, CPU, memory, storage, network, notes.
    """
    profile = DEVICE_PROFILE_MANAGER.get_profile(device_key)
    if not profile:
        return f"No profile found for '{device_key}'."
    lines = [f"Device Profile: {device_key}"]
    for k in ["hostname", "os", "kernel", "architecture", "cpu", "memory", "storage", "network", "notes"]:
        v = profile.get(k)
        if v:
            lines.append(f"  {k}: {v}")
    return "\n".join(lines)


@mcp.tool()
def list_device_profiles() -> str:
    """List all stored device profiles (device memory).

    Returns:
        Device keys with hostname, OS, kernel, architecture, and last update time.
    """
    profiles = DEVICE_PROFILE_MANAGER.list_profiles()
    if not profiles:
        return "No device profiles stored."
    lines = [f"Device Profiles: {len(profiles)}"]
    for profile in sorted(profiles, key=lambda p: p.get("updated_at", ""), reverse=True):
        identity = " | ".join(
            str(profile.get(key)) for key in ("hostname", "os", "kernel", "architecture")
            if profile.get(key)
        ) or "-"
        lines.append(f"  {profile.get('device_key', '?')}: {identity} | {profile.get('updated_at', '-')}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="sshhelper MCP server")
    parser.add_argument("--http", action="store_true",
                        help="serve streamable HTTP instead of stdio")
    parser.add_argument("--host", default="127.0.0.1",
                        help="HTTP bind address (default 127.0.0.1)")
    parser.add_argument("--port", type=int, default=8787,
                        help="HTTP port (default 8787)")
    args = parser.parse_args()

    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()  # stdio, for Codex / Claude Desktop / Cursor / Cline
