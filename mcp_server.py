"""MCP Server -- exposes device-operation tools via the Model Context Protocol.

This module wraps the system's core capabilities as MCP tools so that any
MCP-compatible AI agent (Claude Desktop, Cursor, Cline, etc.) can discover and
call them directly -- no HTTP, no OpenAPI spec parsing required.

Architecture: each MCP tool calls the same underlying modules (DEVICE_MANAGER,
industrial clients, VAULT, GROUP_MANAGER, BASELINE_MANAGER) as the web UI and
the RESTful external_api. Zero logic duplication.

Run standalone:
    python mcp_server.py          # stdio transport (for Claude Desktop, etc.)
    python mcp_server.py --http   # HTTP transport (for remote agents)

Or import the server object:
    from mcp_server import mcp
"""
import os
import sys

# Ensure the project root is on the path when run as a standalone script.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from fastmcp import FastMCP
from typing import Optional

from tools import DEVICE_MANAGER, DEVICE_PROFILE_MANAGER, _clean_terminal_output, _run_on_one_device
from vault import VAULT
from device_groups import GROUP_MANAGER
from baseline import BASELINE_MANAGER, DEFAULT_PROBES
import audit
import paramiko

mcp = FastMCP("Hardware Debugging Tools")


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
            return f"Exit Status: {exit_status}\n{output.strip()[:8000]}"
        except Exception as e:
            client.close()
            return f"Error: {e}"
    except Exception as e:
        return f"Connection error: {e}"


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
    audit.record(session_id=None, device=f"ssh:{host}", command=command[:200], source="mcp")
    return _exec_ssh(host, command, username, password or None, port)


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
def upload_file(host: str, local_path: str, remote_path: str,
                username: str = "root", password: str = "", port: int = 22) -> str:
    """Upload a file to a device via SFTP.

    Args:
        host: Device IP address.
        local_path: Path to the file on this server.
        remote_path: Destination path on the device.
        username: SSH username (default 'root').
        password: SSH password (leave empty for vault).
        port: SSH port (default 22).

    Returns:
        Upload confirmation with file size.
    """
    if not os.path.isfile(local_path):
        return f"Error: Local file not found: {local_path}"
    temp_sid = f"mcp_upload_{host}"
    try:
        DEVICE_MANAGER.connect_ssh(host, username, password or None, port, session_id=temp_sid)
        return DEVICE_MANAGER.upload_file(local_path, remote_path, session_id=temp_sid)
    except Exception as e:
        return f"Error: {e}"
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@mcp.tool()
def download_file(host: str, remote_path: str, local_path: str,
                  username: str = "root", password: str = "", port: int = 22) -> str:
    """Download a file from a device via SFTP.

    Args:
        host: Device IP address.
        remote_path: Path on the device to fetch.
        local_path: Where to save the file on this server.
        username: SSH username (default 'root').
        password: SSH password (leave empty for vault).
        port: SSH port (default 22).

    Returns:
        Download confirmation with file size.
    """
    temp_sid = f"mcp_dl_{host}"
    try:
        DEVICE_MANAGER.connect_ssh(host, username, password or None, port, session_id=temp_sid)
        return DEVICE_MANAGER.download_file(remote_path, local_path, session_id=temp_sid)
    except Exception as e:
        return f"Error: {e}"
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


@mcp.tool()
def reboot_device(host: str, username: str = "root", password: str = "",
                  port: int = 22, wait_seconds: int = 60) -> str:
    """Reboot a device and wait for it to come back online, then verify.

    Args:
        host: Device IP address.
        username: SSH username (default 'root').
        password: SSH password (leave empty for vault).
        port: SSH port (default 22).
        wait_seconds: Max seconds to wait for the device to return (default 60).

    Returns:
        Confirmation that the device rebooted and reconnected, or a timeout error.
    """
    temp_sid = f"mcp_reboot_{host}"
    try:
        DEVICE_MANAGER.connect_ssh(host, username, password or None, port, session_id=temp_sid)
        conn = DEVICE_MANAGER.get_connection(temp_sid)
        try:
            conn.ssh_client.exec_command("nohup sh -c 'sleep 1; reboot' >/dev/null 2>&1 &", timeout=5)
        except Exception:
            pass
        ok = DEVICE_MANAGER.reconnect(session_id=temp_sid, timeout=wait_seconds, poll_interval=5.0)
        if ok:
            result = DEVICE_MANAGER.execute("echo alive; uptime", session_id=temp_sid)
            return f"Device rebooted and reconnected successfully.\n{result[:500]}"
        return f"Error: Device did not come back within {wait_seconds}s."
    except Exception as e:
        return f"Error: {e}"
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


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
def snapshot_config(host: str, username: str = "root", password: str = "",
                    port: int = 22) -> str:
    """Capture a configuration baseline snapshot of a device.

    Records ip addr, iptables, routes, mounts, running services, SSH config, etc.
    into a timestamped snapshot for later drift comparison.

    Args:
        host: Device IP address.
        username: SSH username (default 'root').
        password: SSH password (leave empty for vault).
        port: SSH port (default 22).

    Returns:
        Confirmation with the snapshot timestamp and probe count.
    """
    temp_sid = f"mcp_snap_{host}"
    try:
        DEVICE_MANAGER.connect_ssh(host, username, password or None, port, session_id=temp_sid)
        probes = {}
        for name, cmd in DEFAULT_PROBES.items():
            try:
                raw = DEVICE_MANAGER.execute(cmd, session_id=temp_sid)
                cleaned = "\n".join(l for l in raw.split("\n")
                                    if not l.startswith(("Exit Status:", "OUTPUT:", "---")))
                probes[name] = cleaned.strip()[:5000]
            except Exception:
                probes[name] = "<error>"
        snapshot = BASELINE_MANAGER.save_snapshot(host, probes)
        return f"Snapshot saved at {snapshot['timestamp']} with {len(probes)} probes for {host}."
    except Exception as e:
        return f"Error: {e}"
    finally:
        DEVICE_MANAGER.disconnect(session_id=temp_sid)


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
    q_lower = query.lower()
    scored = []
    for c in cases:
        score = 0
        if q_lower in c.get("title", "").lower():
            score += 10
        for t in c.get("tags", []):
            if q_lower in t.lower() or t.lower() in q_lower:
                score += 5
        for sq in c.get("search_queries", []):
            if q_lower in sq.lower() or sq.lower() in q_lower:
                score += 3
        try:
            with open(c["path"], "r", encoding="utf-8") as f:
                if q_lower in f.read().lower():
                    score += 1
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


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    transport = "stdio"
    if "--http" in sys.argv:
        transport = "http"
    if transport == "stdio":
        mcp.run()  # stdio by default, for Claude Desktop / Cursor / Cline
    else:
        mcp.run(transport="http", host="0.0.0.0", port=8787)