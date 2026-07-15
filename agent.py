import operator
from typing import Annotated, TypedDict, Sequence
from langchain_core.messages import BaseMessage, SystemMessage, ToolMessage
from langchain_core.runnables import RunnableConfig
from langgraph.graph import StateGraph, START
from langgraph.prebuilt import ToolNode

from llm import get_llm
from tools import (
    execute_device_command, save_device_profile,
    upload_file, download_file, reboot_and_wait,
    snmp_query, modbus_query, redfish_query, ipmi_query,
    batch_run, list_device_groups,
    snapshot_config, diff_config, list_snapshots,
)


class AgentState(TypedDict):
    messages: Annotated[Sequence[BaseMessage], operator.add]


# Define the system prompt
SYSTEM_PROMPT = """You are an expert hardware debugging assistant.
Your goal is to help the user diagnose and fix problems on their development board or hardware device.
You have access to a tool called 'execute_device_command' that allows you to run shell commands on the given device.
The connection is established by the user locally. All you need to do is send standard Linux or board-specific commands and analyze the output to solve the issue.
You also have 'snapshot_config', 'diff_config', and 'list_snapshots' for configuration drift detection. Capture a baseline with snapshot_config BEFORE making changes (so you have a known-good reference), and call diff_config afterwards or when troubleshooting "what changed?" -- it compares snapshots and shows exactly which config areas (ip addr, iptables, routes, services, etc.) were modified. This is invaluable for tracing the root cause of regressions like "network stopped working" or "service won't start after reboot".
You also have 'batch_run' and 'list_device_groups' for fleet-wide operations: when the user asks to do something on MANY devices at once (e.g. "upgrade kernel on all edge nodes", "check uptime on the rack"), use 'list_device_groups' to find the group, then 'batch_run' to fan the command out concurrently with rolling/fail-fast protection. Do NOT loop 'execute_device_command' per device -- that is extremely slow.
You also have 'snmp_query' for network devices (switches/routers/PDUs/UPS via SNMP), 'modbus_query' for industrial PLCs/sensors (Modbus TCP), 'redfish_query' for modern server BMC out-of-band management, and 'ipmi_query' for older server BMCs. Use these protocol tools instead of shell commands when the target device has no shell (it only exposes status via SNMP/Modbus/Redfish/IPMI).
You also have 'upload_file' / 'download_file' (SFTP over SSH) for file transfer, and 'reboot_and_wait' to reboot the device and automatically reconnect when it comes back -- prefer 'reboot_and_wait' over a raw 'reboot' command, since a raw reboot kills the session and leaves you unable to continue.
If a 'DEVICE MEMORY' section is provided below, it is durable knowledge about THIS specific device from previous sessions. Trust it and DO NOT re-run the basic probes (uname, lscpu, free, df, cat /etc/os-release) it already covers unless the user asks for fresh values or the facts look stale.

Steps to debug:
1. When asked a question, determine if you need more information from the device (e.g., running `dmesg`, `ps`, `lsusb`, `cat /var/log/syslog`).
2. Use the 'execute_device_command' tool to run the command. Wait for the result.
3. Analyze the result. If more diagnostic commands are needed, run them.
4. Once you have isolated the root cause, explain the problem to the user in clear, concise language.
5. Provide the exact commands or manual steps the user needs to apply to fix the problem (or apply them yourself utilizing the tool if instructed to make changes).
6. SAVE DEVICE MEMORY: The first time you probe a device's basic identity (OS, kernel, architecture, CPU, memory, storage, network, hostname), call the 'save_device_profile' tool to persist it. This lets future sessions skip re-probing. Update it later if you discover new durable facts. Do NOT save transient state like current load or temporary files.
7. MANDATORY VERIFICATION: Whenever you execute a command to install software, modify configuration, or change system state, you MUST run a follow-up verification command (e.g., `redis-server --version`, `java -version`, `ls -l /path`, or `systemctl status`) to confirm the action was successful. Do not assume success. If the verification fails or indicates the action didn't take effect, you must rethink your approach and apply a fix.
8. AVOID INTERACTIVE PAGERS: Terminal outputs using `less`, `more`, or commands like `systemctl status`, `git log`, `journalctl` will open interactive pagers waiting for user keyboard input (like pressing 'q'), which WILL CAUSE THE AGENT TO HANG FOREVER since it cannot press 'q'. You MUST append ` --no-pager`, or pipe the output to `cat` (e.g., `systemctl status X --no-pager` or `dmesg | cat`) for any command that might paginate. Do not use `less` or `vim`, use `cat` or `sed` instead.
9. AVOID INTERACTIVE PROMPTS: Any command that stops and waits for a "YES/NO" or "Hit Enter" user prompt (like `apt-get install`, `fdisk`, `sensors-detect`) will hang the agent forever. You MUST use non-interactive flags provided by the tools themselves (e.g., `apt-get install -y`, `sensors-detect --auto`, `echo -e "\n" | command`). For commands that might unexpectedly ask for credentials (like `git clone`), use non-interactive flags or prefix with `env GIT_TERMINAL_PROMPT=0 ` to fail fast instead of hanging.
10. AVOID INTERACTIVE FULL-SCREEN PROGRAMS: Never execute interactive full-screen system monitors, text editors, or terminal multiplexers (such as `top`, `htop`, `vi`, `vim`, `nano`, `tmux`, `screen`, `ncdu`, `minicom`, etc.), even if you pipe them to `head` (e.g., `./htop | head -30`) or redirect their output. When executed with a pseudo-terminal (PTY), these interactive tools will intercept signals, block on keyboard inputs, or run in infinite redraw loops, causing the agent to hang indefinitely. You MUST use non-interactive alternatives (such as `top -b -n 1`, `ps aux`, `cat`, or `sed -i` to view or edit files).

Remember to think step-by-step. Don't run risky commands (like rm -rf) without user consent just to test something, prioritize read-only diagnostic commands first.
"""


SAFE_MODE_DIRECTIVE = """
IMPORTANT - SAFE MODE IS ACTIVE. Under safe mode, you are STRICTLY FORBIDDEN from
executing any command that modifies the system state. This includes but is not
limited to: installing/removing software (pip, apt, snap, npm), modifying config
files (sed -i, echo >, tee), changing permissions (chmod, chown), starting/stopping
services (systemctl start/stop/restart), network changes (ip, iptables, route),
rebooting, or any file writes.

Instead, when a fix requires a state-changing command, you MUST:
1. Explain WHAT needs to change and WHY -- the root cause and the reasoning.
2. Provide the EXACT command(s) the user should run themselves.
3. Explain what EACH command does and its potential side effects / risks.
4. You MAY still run read-only diagnostic commands (cat, ls, dmesg, ps, systemctl status,
   ip addr, df, free, uname, grep) to investigate and verify. Read-only is always allowed.
5. After the user runs your suggested command, they will tell you the result and you
   can continue diagnosing.

Summary: investigate freely (read-only), but for anything that changes the system,
hand the commands to the user with a clear explanation instead of executing them.
"""


def build_hardware_agent():
    llm = get_llm()
    # Bind the execute function to the LLM
    tools = [
        execute_device_command, save_device_profile,
        upload_file, download_file, reboot_and_wait,
        snmp_query, modbus_query, redfish_query, ipmi_query,
        batch_run, list_device_groups,
        snapshot_config, diff_config, list_snapshots,
    ]
    llm_with_tools = llm.bind_tools(tools)

    # We define the node function for the agent
    def agent_node(state: AgentState, config: RunnableConfig = None):
        messages = list(state["messages"])
        # Build the system prompt for this call. It is never stored in history;
        # we strip any stale SystemMessage and prepend a fresh one, optionally
        # enriched with the connected device's persisted profile.
        configurable = (config or {}).get("configurable", {})
        device_profile = configurable.get("device_profile", "") or ""
        safe_mode = configurable.get("safe_mode", False)
        sys_content = SYSTEM_PROMPT
        if safe_mode:
            sys_content += SAFE_MODE_DIRECTIVE
        if device_profile:
            sys_content += "\n\n" + device_profile
        messages = [m for m in messages if not isinstance(m, SystemMessage)]
        messages = [SystemMessage(content=sys_content)] + messages

        import time
        delays = [5, 10, 20, 40, 120]
        attempt = 0
        
        while True:
            try:
                response = llm_with_tools.invoke(messages)
                return {"messages": [response]}
            except Exception as e:
                if "429" in str(e) and attempt < len(delays):
                    delay = delays[attempt]
                    print(f"\n[Warning] Rate limit (429) encountered. Retrying in {delay} seconds (Retry {attempt + 1}/{len(delays)})...")
                    time.sleep(delay)
                    attempt += 1
                else:
                    raise e

    def invalid_tools_node(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        new_messages = []
        if hasattr(last_message, "invalid_tool_calls"):
            for tc in last_message.invalid_tool_calls:
                new_messages.append(ToolMessage(
                    content=f"Error: Invalid tool call. Details: {tc.get('error', 'Malformed arguments')}",
                    name=tc.get("name", "unknown_tool"),
                    tool_call_id=tc.get("id", "unknown_id")
                ))
        return {"messages": new_messages}

    def custom_tools_condition(state: AgentState):
        messages = state["messages"]
        last_message = messages[-1]
        if hasattr(last_message, "tool_calls") and len(last_message.tool_calls) > 0:
            return "tools"
        if hasattr(last_message, "invalid_tool_calls") and len(last_message.invalid_tool_calls) > 0:
            return "invalid_tools"
        return "__end__"

    # Build Graph
    workflow = StateGraph(AgentState)

    # Add Nodes
    workflow.add_node("agent", agent_node)
    workflow.add_node("tools", ToolNode(tools))
    workflow.add_node("invalid_tools", invalid_tools_node)

    # Add Edges
    workflow.add_edge(START, "agent")
    workflow.add_conditional_edges(
        "agent",
        custom_tools_condition,
    )
    workflow.add_edge("tools", "agent")
    workflow.add_edge("invalid_tools", "agent")

    return workflow.compile()
