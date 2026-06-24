"""
Tests for the SshTunnel port-forward manager.

These use mocked subprocess/socket plumbing so no real ssh process is spawned.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import ssh_tunnel  # noqa: E402
from ssh_tunnel import SshTunnel  # noqa: E402


class TestBuildCommand:
    def test_basic_forward_and_target(self):
        tunnel = SshTunnel(host="runner1", user="ci", remote_adb_port=5037)
        cmd = tunnel.build_command(15037)
        assert cmd[0] == "ssh"
        assert "-N" in cmd
        # Local forward maps local port -> remote adb server.
        assert "15037:127.0.0.1:5037" in cmd
        # Target is the last argument.
        assert cmd[-1] == "ci@runner1"
        # Key-based, non-interactive by default.
        assert "BatchMode=yes" in cmd

    def test_strict_host_key_is_configurable(self):
        tunnel = SshTunnel(host="h", strict_host_key="yes")
        cmd = tunnel.build_command(1234)
        assert "StrictHostKeyChecking=yes" in cmd

    def test_key_path_adds_identity_options(self):
        tunnel = SshTunnel(host="h", key_path="/keys/id_ed25519")
        cmd = tunnel.build_command(1234)
        assert "-i" in cmd
        assert "/keys/id_ed25519" in cmd
        assert "IdentitiesOnly=yes" in cmd

    def test_no_user_uses_bare_host(self):
        tunnel = SshTunnel(host="justhost")
        assert tunnel.target == "justhost"
        assert tunnel.build_command(1)[-1] == "justhost"

    def test_custom_ssh_port(self):
        tunnel = SshTunnel(host="h", port=2222)
        cmd = tunnel.build_command(1)
        assert "-p" in cmd and "2222" in cmd


class TestStartStop:
    @patch("ssh_tunnel._port_open", return_value=True)
    @patch("ssh_tunnel._find_free_port", return_value=15037)
    @patch("ssh_tunnel.subprocess.Popen")
    def test_start_returns_local_port_when_forward_ready(
        self, mock_popen, mock_free_port, mock_port_open
    ):
        proc = MagicMock()
        proc.poll.return_value = None  # process stays alive
        mock_popen.return_value = proc

        tunnel = SshTunnel(host="h", user="u")
        port = tunnel.start()

        assert port == 15037
        assert tunnel.local_port == 15037
        assert tunnel.is_alive() is True

    @patch("ssh_tunnel._find_free_port", return_value=15037)
    @patch("ssh_tunnel.subprocess.Popen")
    def test_start_raises_when_ssh_exits(self, mock_popen, mock_free_port):
        proc = MagicMock()
        proc.poll.return_value = 255  # ssh failed immediately
        proc.returncode = 255
        proc.stdout.read.return_value = "Permission denied (publickey)."
        mock_popen.return_value = proc

        tunnel = SshTunnel(host="h", user="u")
        with pytest.raises(RuntimeError, match="Permission denied"):
            tunnel.start(wait_timeout=2)

    @patch("ssh_tunnel._port_open", return_value=True)
    @patch("ssh_tunnel._find_free_port", return_value=15037)
    @patch("ssh_tunnel.subprocess.Popen")
    def test_stop_terminates_process(
        self, mock_popen, mock_free_port, mock_port_open
    ):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc

        tunnel = SshTunnel(host="h")
        tunnel.start()
        tunnel.stop()

        proc.terminate.assert_called_once()
        assert tunnel.local_port is None
        assert tunnel.is_alive() is False

    @patch("ssh_tunnel._port_open", return_value=True)
    @patch("ssh_tunnel._find_free_port", return_value=15037)
    @patch("ssh_tunnel.subprocess.Popen")
    def test_start_is_idempotent_while_alive(
        self, mock_popen, mock_free_port, mock_port_open
    ):
        proc = MagicMock()
        proc.poll.return_value = None
        mock_popen.return_value = proc

        tunnel = SshTunnel(host="h")
        assert tunnel.start() == 15037
        assert tunnel.start() == 15037  # second call does not respawn
        mock_popen.assert_called_once()
