"""
Tests for the MCP server wiring (lazy device initialization).

These guard the invariant that the server process must always start — even
with no device connected, the wrong device, or adb missing — and only surface
those conditions as per-call errors. Regressing this takes down every tool.
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Add the parent directory to the path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402


class TestServerLazyInitialization:
    """The server must come up regardless of device state."""

    def setup_method(self):
        """Reset connection state before each test."""
        server._device_manager = None
        server._ssh_tunnel = None
        server._persisted_ssh = None
        server._adb_endpoint = server.DEFAULT_ADB
        server.device_name = None

    def test_import_does_not_construct_manager(self):
        """Importing the server must not eagerly create a device manager.

        Eager construction is what previously called sys.exit(1) at startup
        when no device was connected, killing the whole MCP server.
        """
        assert server._device_manager is None
        assert server.mcp is not None

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices")
    def test_list_devices_with_no_devices(self, mock_get_devices):
        """list_devices works (does not raise) when nothing is connected."""
        mock_get_devices.return_value = []
        assert server.list_devices() == "No devices connected."

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices")
    def test_list_devices_lists_serials(self, mock_get_devices):
        """list_devices reports every connected serial, one per line."""
        mock_get_devices.return_value = ["device123", "emulator-5554"]
        assert server.list_devices() == "device123\nemulator-5554"

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices")
    def test_list_devices_reports_adb_failure_gracefully(self, mock_get_devices):
        """A missing/broken adb yields a message, not an unhandled crash."""
        mock_get_devices.side_effect = FileNotFoundError("adb")
        result = server.list_devices()
        assert "Unable to query devices" in result

    @patch("adbdevicemanager.AdbDeviceManager.check_adb_installed", return_value=True)
    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices", return_value=[])
    def test_device_tool_errors_when_no_device(self, mock_get_devices, mock_check):
        """A device-dependent tool raises (becomes a tool error) — not a crash.

        And the failed attempt is not cached, so a later call retries.
        """
        with pytest.raises(RuntimeError, match="No devices connected"):
            server.get_device_manager()
        assert server._device_manager is None  # failure not cached

    @patch("adbdevicemanager.AdbClient")
    @patch("adbdevicemanager.AdbDeviceManager.check_adb_installed", return_value=True)
    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices",
           return_value=["device123"])
    def test_device_tool_lazily_creates_manager(
        self, mock_get_devices, mock_check, mock_adb_client
    ):
        """First device-dependent call constructs and caches the manager."""
        mock_device = MagicMock()
        mock_adb_client.return_value.device.return_value = mock_device

        manager = server.get_device_manager()

        assert manager.device == mock_device
        assert server._device_manager is manager
        # Second call reuses the cached instance.
        assert server.get_device_manager() is manager


class TestSshConnection:
    """Wiring of the SSH-over-adb tools (tunnel itself is mocked)."""

    def setup_method(self):
        server._device_manager = None
        server._ssh_tunnel = None
        server._persisted_ssh = None
        server._adb_endpoint = server.DEFAULT_ADB
        server.device_name = None

    def _fake_tunnel(self, local_port=15037, target="ci@runner1"):
        tunnel = MagicMock()
        tunnel.start.return_value = local_port
        tunnel.local_port = local_port
        tunnel.is_alive.return_value = True
        tunnel.target = target
        return tunnel

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices",
           return_value=["emulator-5554"])
    @patch("server.SshTunnel")
    def test_connect_ssh_points_adb_at_tunnel(self, mock_ssh, mock_get_devices):
        mock_ssh.return_value = self._fake_tunnel()

        result = server.connect_ssh(host="runner1", user="ci", persist=False)

        assert server._adb_endpoint == ("127.0.0.1", 15037)
        assert server._ssh_tunnel is not None
        assert "emulator-5554" in result
        assert "runner1" in result

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices",
           return_value=["emulator-5554"])
    @patch("server.SshTunnel")
    def test_connect_ssh_preselects_device(self, mock_ssh, mock_get_devices):
        mock_ssh.return_value = self._fake_tunnel()

        server.connect_ssh(host="runner1", user="ci",
                           device="emulator-5554", persist=False)

        assert server.device_name == "emulator-5554"
        assert server._persisted_ssh["device"] == "emulator-5554"

    @patch("server._write_config")
    @patch("server._read_config", return_value={})
    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices",
           return_value=["emulator-5554"])
    @patch("server.SshTunnel")
    def test_connect_ssh_persists_config(
        self, mock_ssh, mock_get_devices, mock_read, mock_write
    ):
        mock_ssh.return_value = self._fake_tunnel()

        server.connect_ssh(host="runner1", user="ci",
                           key_path="/keys/id", persist=True)

        mock_write.assert_called_once()
        written = mock_write.call_args[0][0]
        assert written["ssh"]["host"] == "runner1"
        assert written["ssh"]["key_path"] == "/keys/id"
        # None-valued fields are not persisted.
        assert "device" not in written["ssh"]

    @patch("server.SshTunnel")
    def test_connect_ssh_failure_is_reported_not_raised(self, mock_ssh):
        tunnel = MagicMock()
        tunnel.start.side_effect = RuntimeError("Permission denied (publickey).")
        mock_ssh.return_value = tunnel

        result = server.connect_ssh(host="runner1", user="ci", persist=False)

        assert "Failed to open SSH tunnel" in result
        assert server._adb_endpoint == server.DEFAULT_ADB  # reverted

    @patch("server._write_config")
    @patch("server._read_config", return_value={"ssh": {"host": "runner1"}})
    def test_disconnect_ssh_reverts_to_local(self, mock_read, mock_write):
        tunnel = self._fake_tunnel()
        server._ssh_tunnel = tunnel
        server._adb_endpoint = ("127.0.0.1", 15037)
        server._persisted_ssh = {"host": "runner1"}

        result = server.disconnect_ssh()

        tunnel.stop.assert_called_once()
        assert server._adb_endpoint == server.DEFAULT_ADB
        assert server._persisted_ssh is None
        assert "local adb server" in result
        mock_write.assert_called_once()  # ssh block removed and rewritten

    def test_ssh_status_reports_active_tunnel(self):
        server._ssh_tunnel = self._fake_tunnel()
        assert "active" in server.ssh_status()

    def test_ssh_status_reports_no_tunnel(self):
        assert "No SSH tunnel" in server.ssh_status()

    @patch("adbdevicemanager.AdbDeviceManager.get_available_devices",
           return_value=["emulator-5554"])
    @patch("server.SshTunnel")
    def test_list_devices_uses_tunnel_when_configured(
        self, mock_ssh, mock_get_devices
    ):
        """list_devices lazily brings up the persisted tunnel, then queries it."""
        mock_ssh.return_value = self._fake_tunnel()
        server._persisted_ssh = {"host": "runner1", "user": "ci"}

        result = server.list_devices()

        assert "emulator-5554" in result
        assert server._adb_endpoint == ("127.0.0.1", 15037)
