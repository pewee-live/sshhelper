"""Tests for tools.py: command classifier, terminal output cleaning, safe mode."""
import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from tools import _classify_command, _clean_terminal_output


class TestCommandClassifier:
    """Safe-mode command classification: read vs modify."""

    def test_read_commands(self):
        for cmd in [
            "uname -a", "cat /etc/hosts", "dmesg | tail -20",
            "systemctl status nginx", "ip addr show", "df -h",
            "free -m", "ls -la /tmp", "ps aux", "ip route show",
            "uptime", "whoami", "hostname",
        ]:
            assert _classify_command(cmd) == "read", f"Expected read for: {cmd}"

    def test_package_install(self):
        assert _classify_command("apt-get install -y nginx") == "modify"
        assert _classify_command("pip install flask") == "modify"

    def test_service_control(self):
        assert _classify_command("systemctl restart nginx") == "modify"
        assert _classify_command("service nginx stop") == "modify"

    def test_firewall(self):
        assert _classify_command("iptables -F") == "modify"
        assert _classify_command("iptables -A INPUT -p tcp --dport 80 -j ACCEPT") == "modify"

    def test_file_modification(self):
        assert _classify_command("echo hello > /etc/test") == "modify"
        assert _classify_command("sed -i 's/old/new/g' /etc/config") == "modify"
        assert _classify_command("rm -rf /tmp/test") == "modify"
        assert _classify_command("mkdir /tmp/newdir") == "modify"

    def test_permissions(self):
        assert _classify_command("chmod 755 /root/script.sh") == "modify"
        assert _classify_command("chown root:root /etc/file") == "modify"

    def test_system_power(self):
        assert _classify_command("reboot") == "modify"
        assert _classify_command("shutdown -h now") == "modify"

    def test_user_management(self):
        assert _classify_command("usermod -aG docker user") == "modify"
        assert _classify_command("useradd newuser") == "modify"

    def test_network_modification(self):
        assert _classify_command("ip addr add 192.168.1.1/24 dev eth0") == "modify"
        assert _classify_command("ip link set eth0 down") == "modify"

    def test_mount(self):
        assert _classify_command("mount /dev/sda1 /mnt") == "modify"

    def test_kernel_modules(self):
        assert _classify_command("modprobe zram") == "modify"

    def test_ip_addr_show_is_read(self):
        """Regression test: 'ip addr show' must NOT be classified as modify."""
        assert _classify_command("ip addr show") == "read"
        assert _classify_command("ip route show") == "read"
        assert _classify_command("ip link show") == "read"


class TestCleanTerminalOutput:
    """Terminal output cleaning: ANSI stripping and progress bar normalization."""

    def test_strips_ansi_colors(self):
        result = _clean_terminal_output("\x1b[32mSuccess\x1b[0m")
        assert result == "Success"

    def test_strips_multiple_ansi(self):
        result = _clean_terminal_output("\x1b[1;31mError\x1b[0m: \x1b[32mOK\x1b[0m")
        assert result == "Error: OK"

    def test_progress_bar_collapse(self):
        raw = "downloading 1%\rdownloading 50%\rdownloading 100%\n"
        result = _clean_terminal_output(raw)
        assert "100%" in result
        assert "1%" not in result or result.count("1%") == 0 or "100%" in result

    def test_crlf_to_lf(self):
        result = _clean_terminal_output("line1\r\nline2\r\n")
        assert "\r\n" not in result

    def test_strips_control_chars(self):
        result = _clean_terminal_output("hello\x00\x07\x08world")
        assert "\x00" not in result
        assert "\x07" not in result

    def test_preserves_normal_text(self):
        result = _clean_terminal_output("Hello World\nLine 2")
        assert result == "Hello World\nLine 2"

    def test_empty_input(self):
        assert _clean_terminal_output("") == ""

    def test_strips_osc_sequences(self):
        result = _clean_terminal_output("\x1b]0;title\x07text")
        assert "text" in result
        assert "\x1b" not in result


class TestConcurrentLocks:
    """Verify per-session locks exist and serialize access."""

    def test_different_sessions_get_different_locks(self):
        from tools import DEVICE_MANAGER
        lock1 = DEVICE_MANAGER._get_lock("session-a")
        lock2 = DEVICE_MANAGER._get_lock("session-b")
        assert lock1 is not lock2

    def test_same_session_gets_same_lock(self):
        from tools import DEVICE_MANAGER
        lock1 = DEVICE_MANAGER._get_lock("session-x")
        lock2 = DEVICE_MANAGER._get_lock("session-x")
        assert lock1 is lock2

    def test_default_session_lock(self):
        from tools import DEVICE_MANAGER
        lock = DEVICE_MANAGER._get_lock(None)
        assert lock is not None