"""
Tests for the UI-interaction and device-state tools, plus the hardened
screenshot / UI-dump file handling (temp files, unique device paths, no fixed
filenames in the working directory).
"""

import os
import sys
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image as PILImage

# Add the parent directory to the path so we can import our modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server  # noqa: E402
from adbdevicemanager import AdbDeviceManager  # noqa: E402


@pytest.fixture
def manager():
    """An AdbDeviceManager whose adb device is a bare mock to assert against."""
    with patch("adbdevicemanager.AdbDeviceManager.check_adb_installed", return_value=True), \
         patch("adbdevicemanager.AdbDeviceManager.get_available_devices", return_value=["dev"]), \
         patch("adbdevicemanager.AdbClient"):
        mgr = AdbDeviceManager(device_name="dev", exit_on_error=False)
    mgr.device = MagicMock()
    return mgr


# --------------------------------------------------------------------------
# UI interaction
# --------------------------------------------------------------------------

class TestUiInteraction:
    def test_tap(self, manager):
        result = manager.tap(10, 20)
        manager.device.shell.assert_called_once_with("input tap 10 20")
        assert "10" in result and "20" in result

    def test_swipe(self, manager):
        manager.swipe(1, 2, 3, 4, duration_ms=250)
        manager.device.shell.assert_called_once_with("input swipe 1 2 3 4 250")

    def test_long_press_is_a_held_swipe_in_place(self, manager):
        manager.long_press(5, 6, duration_ms=700)
        manager.device.shell.assert_called_once_with("input swipe 5 6 5 6 700")

    def test_input_text_encodes_spaces_as_percent_s(self, manager):
        manager.input_text("hello world")
        manager.device.shell.assert_called_once_with("input text 'hello%sworld'")

    def test_input_text_escapes_single_quotes(self, manager):
        manager.input_text("it's")
        manager.device.shell.assert_called_once_with("input text 'it'\\''s'")

    def test_press_key_alias(self, manager):
        manager.press_key("back")
        manager.device.shell.assert_called_once_with("input keyevent KEYCODE_BACK")

    def test_press_key_numeric_passthrough(self, manager):
        manager.press_key("66")
        manager.device.shell.assert_called_once_with("input keyevent 66")

    def test_press_key_full_keycode_passthrough(self, manager):
        manager.press_key("KEYCODE_ENTER")
        manager.device.shell.assert_called_once_with("input keyevent KEYCODE_ENTER")

    def test_launch_app_resolves_and_starts_component(self, manager):
        manager.device.shell.side_effect = [
            "priority=0 preferredOrder=0\ncom.waggr/.MainActivity",  # resolve
            "Starting: Intent { ... }",                              # am start
        ]
        result = manager.launch_app("com.waggr")
        assert "com.waggr/.MainActivity" in result
        manager.device.shell.assert_any_call("am start -n com.waggr/.MainActivity")

    def test_launch_app_falls_back_to_monkey(self, manager):
        manager.device.shell.side_effect = ["No activity found", ""]
        result = manager.launch_app("com.waggr")
        assert "monkey fallback" in result
        assert any("monkey -p com.waggr" in c.args[0]
                   for c in manager.device.shell.call_args_list)


# --------------------------------------------------------------------------
# Device state / lifecycle
# --------------------------------------------------------------------------

class TestDeviceState:
    def test_get_current_app_parses_resumed_activity(self, manager):
        manager.device.shell.return_value = (
            "  mResumedActivity: ActivityRecord{abc123 u0 "
            "com.waggr/.MainActivity t42}\n")
        assert manager.get_current_app() == "com.waggr/.MainActivity"

    def test_get_current_app_unknown(self, manager):
        manager.device.shell.return_value = "nothing useful here"
        assert "Unable to determine" in manager.get_current_app()

    def test_device_info(self, manager):
        mapping = {
            "getprop ro.product.manufacturer": "Google",
            "getprop ro.product.model": "Pixel 7",
            "getprop ro.build.version.release": "14",
            "getprop ro.build.version.sdk": "34",
            "getprop ro.build.display.id": "UP1A.231005",
            "getprop ro.product.cpu.abi": "arm64-v8a",
            "wm size": "Physical size: 1080x2400",
            "wm density": "Physical density: 420",
            "dumpsys battery": "  level: 87\n  scale: 100",
        }
        manager.device.shell.side_effect = lambda cmd: mapping.get(cmd, "")
        info = manager.device_info()
        assert "model: Pixel 7" in info
        assert "android_version: 14" in info
        assert "1080x2400" in info
        assert "battery level: 87" in info

    def test_install_apk_missing_path_raises(self, manager):
        with patch("os.path.exists", return_value=False):
            with pytest.raises(RuntimeError, match="APK not found"):
                manager.install_apk("/no/such.apk")

    def test_install_apk_success(self, manager):
        manager.device.install = MagicMock()
        with patch("os.path.exists", return_value=True):
            result = manager.install_apk("/tmp/app.apk", reinstall=True)
        manager.device.install.assert_called_once_with("/tmp/app.apk", reinstall=True)
        assert "app.apk" in result

    def test_uninstall_success(self, manager):
        manager.device.shell.return_value = "Success"
        assert manager.uninstall_app("com.waggr") == "Uninstalled com.waggr"

    def test_uninstall_reports_failure_text(self, manager):
        manager.device.shell.return_value = "Failure [DELETE_FAILED_INTERNAL_ERROR]"
        assert "DELETE_FAILED" in manager.uninstall_app("com.waggr")

    def test_force_stop(self, manager):
        manager.force_stop("com.waggr")
        manager.device.shell.assert_called_once_with("am force-stop com.waggr")

    def test_set_clipboard_quotes_text(self, manager):
        manager.set_clipboard("hi there")
        manager.device.shell.assert_called_once_with(
            "cmd clipboard set-text 'hi there'")

    def test_get_clipboard_empty(self, manager):
        manager.device.shell.return_value = ""
        assert "unavailable" in manager.get_clipboard()


# --------------------------------------------------------------------------
# Hardened file handling
# --------------------------------------------------------------------------

class TestHardenedFileHandling:
    def test_take_screenshot_returns_png_bytes_and_cleans_up(self, manager):
        pulled = {}

        def fake_pull(remote, local):
            pulled["remote"] = remote
            pulled["local"] = local
            PILImage.new("RGB", (100, 200), "red").save(local, format="PNG")

        manager.device.pull.side_effect = fake_pull

        data = manager.take_screenshot(scale=0.5)

        # Real PNG bytes returned (no dependency on a file in the cwd).
        assert data[:8] == b"\x89PNG\r\n\x1a\n"
        # Unique device path under /sdcard, not the old fixed screenshot.png.
        assert pulled["remote"].startswith("/sdcard/mcp_screenshot_")
        screencap_calls = [c.args[0] for c in manager.device.shell.call_args_list
                           if "screencap" in c.args[0]]
        assert screencap_calls and pulled["remote"] in screencap_calls[0]
        # Local temp file is removed afterwards (no artifact left behind).
        assert not os.path.exists(pulled["local"])

    def test_get_uilayout_uses_temp_files_and_parses(self, manager):
        sample_xml = (
            '<?xml version="1.0" encoding="UTF-8"?>'
            '<hierarchy>'
            '<node clickable="true" text="Login" content-desc="" '
            'bounds="[0,0][100,50]"/>'
            '<node clickable="false" text="ignored" bounds="[0,0][10,10]"/>'
            '</hierarchy>'
        )
        pulled = {}

        def fake_pull(remote, local):
            pulled["local"] = local
            with open(local, "w", encoding="utf-8") as f:
                f.write(sample_xml)

        manager.device.pull.side_effect = fake_pull
        manager.device.shell.return_value = "UI hierchary dumped to: ..."

        result = manager.get_uilayout()

        assert "Text: Login" in result
        assert "Center: (50, 25)" in result
        assert "ignored" not in result
        dump_calls = [c.args[0] for c in manager.device.shell.call_args_list
                      if c.args[0].startswith("uiautomator dump ")]
        assert dump_calls and "/sdcard/mcp_uidump_" in dump_calls[0]
        assert not os.path.exists(pulled["local"])

    def test_get_uilayout_reports_dump_error(self, manager):
        manager.device.shell.return_value = "ERROR: could not get idle state."
        assert "Failed to dump UI hierarchy" in manager.get_uilayout()


# --------------------------------------------------------------------------
# Server tool wiring (delegation to the device manager)
# --------------------------------------------------------------------------

class TestServerToolWiring:
    def _stub_manager(self, monkeypatch):
        mgr = MagicMock()
        monkeypatch.setattr(server, "get_device_manager", lambda: mgr)
        return mgr

    def test_tap_delegates(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        mgr.tap.return_value = "Tapped (1, 2)"
        assert server.tap(1, 2) == "Tapped (1, 2)"
        mgr.tap.assert_called_once_with(1, 2)

    def test_input_text_delegates(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        server.input_text("hello")
        mgr.input_text.assert_called_once_with("hello")

    def test_go_home_presses_home(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        server.go_home()
        mgr.press_key.assert_called_once_with("HOME")

    def test_go_back_presses_back(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        server.go_back()
        mgr.press_key.assert_called_once_with("BACK")

    def test_install_apk_delegates(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        server.install_apk("/tmp/a.apk", reinstall=False)
        mgr.install_apk.assert_called_once_with("/tmp/a.apk", False)

    def test_get_screenshot_returns_png_image(self, monkeypatch):
        mgr = self._stub_manager(monkeypatch)
        mgr.take_screenshot.return_value = b"\x89PNG\r\n\x1a\nfakedata"
        img = server.get_screenshot()
        assert img.data == b"\x89PNG\r\n\x1a\nfakedata"
        assert img._mime_type == "image/png"
