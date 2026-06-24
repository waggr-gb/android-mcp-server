import os
import re
import subprocess
import sys
import tempfile
import uuid
import xml.etree.ElementTree as ET
from io import BytesIO

from PIL import Image as PILImage
from ppadb.client import Client as AdbClient


class AdbDeviceManager:
    def __init__(self, device_name: str | None = None, exit_on_error: bool = True,
                 host: str = "127.0.0.1", port: int = 5037) -> None:
        """
        Initialize the ADB Device Manager

        Args:
            device_name: Optional name/serial of the device to manage.
                         If None, attempts to auto-select if only one device is available.
            exit_on_error: Whether to exit the program if device initialization fails
            host: Host of the adb server to talk to. Defaults to the local adb
                  server; point it at a forwarded port to drive a remote device
                  (e.g. over an SSH tunnel).
            port: Port of the adb server to talk to (default 5037).
        """
        self.host = host
        self.port = port

        if not self.check_adb_installed():
            error_msg = "adb is not installed or not in PATH. Please install adb and ensure it is in your PATH."
            if exit_on_error:
                print(error_msg, file=sys.stderr)
                sys.exit(1)
            else:
                raise RuntimeError(error_msg)

        available_devices = self.get_available_devices(host, port)
        if not available_devices:
            error_msg = "No devices connected. Please connect a device and try again."
            if exit_on_error:
                print(error_msg, file=sys.stderr)
                sys.exit(1)
            else:
                raise RuntimeError(error_msg)

        selected_device_name: str | None = None

        if device_name:
            if device_name not in available_devices:
                error_msg = f"Device {device_name} not found. Available devices: {available_devices}"
                if exit_on_error:
                    print(error_msg, file=sys.stderr)
                    sys.exit(1)
                else:
                    raise RuntimeError(error_msg)
            selected_device_name = device_name
        else:  # No device_name provided, try auto-selection
            if len(available_devices) == 1:
                selected_device_name = available_devices[0]
                print(
                    f"No device specified, automatically selected: {selected_device_name}")
            elif len(available_devices) > 1:
                error_msg = f"Multiple devices connected: {available_devices}. Please specify a device in config.yaml or connect only one device."
                if exit_on_error:
                    print(error_msg, file=sys.stderr)
                    sys.exit(1)
                else:
                    raise RuntimeError(error_msg)
            # If len(available_devices) == 0, it's already caught by the earlier check

        # At this point, selected_device_name should always be set due to the logic above
        # Initialize the device
        self.device = AdbClient(host=host, port=port).device(selected_device_name)

    @staticmethod
    def check_adb_installed() -> bool:
        """Check if ADB is installed on the system."""
        try:
            subprocess.run(["adb", "version"], check=True,
                           stdout=subprocess.PIPE)
            return True
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    @staticmethod
    def get_available_devices(host: str = "127.0.0.1", port: int = 5037) -> list[str]:
        """Get a list of available devices from the given adb server."""
        return [device.serial for device in AdbClient(host=host, port=port).devices()]

    def get_packages(self) -> str:
        command = "pm list packages"
        packages = self.device.shell(command).strip().split("\n")
        result = [package[8:] for package in packages]
        output = "\n".join(result)
        return output

    def get_package_action_intents(self, package_name: str) -> list[str]:
        command = f"dumpsys package {package_name}"
        output = self.device.shell(command)

        resolver_table_start = output.find("Activity Resolver Table:")
        if resolver_table_start == -1:
            return []
        resolver_section = output[resolver_table_start:]

        non_data_start = resolver_section.find("\n  Non-Data Actions:")
        if non_data_start == -1:
            return []

        section_end = resolver_section[non_data_start:].find("\n\n")
        if section_end == -1:
            non_data_section = resolver_section[non_data_start:]
        else:
            non_data_section = resolver_section[
                non_data_start: non_data_start + section_end
            ]

        actions = []
        for line in non_data_section.split("\n"):
            line = line.strip()
            if line.startswith("android.") or line.startswith("com."):
                actions.append(line)

        return actions

    def execute_adb_shell_command(self, command: str) -> str:
        """Executes an ADB command and returns the output."""
        if command.startswith("adb shell "):
            command = command[10:]
        elif command.startswith("adb "):
            command = command[4:]
        result = self.device.shell(command)
        return result

    def take_screenshot(self, scale: float = 0.3) -> bytes:
        """Capture the screen and return compressed PNG bytes.

        Uses a unique on-device path and a private local temp file (never fixed
        names in the working directory) and removes both afterwards, so
        concurrent calls don't collide and nothing is left in the repo dir.
        """
        token = uuid.uuid4().hex
        remote = f"/sdcard/mcp_screenshot_{token}.png"
        fd, local_path = tempfile.mkstemp(prefix="mcp_screenshot_", suffix=".png")
        os.close(fd)
        try:
            self.device.shell(f"screencap -p {remote}")
            self.device.pull(remote, local_path)
            # Compress to keep the payload small (avoids "maximum call stack
            # exceeded" on some clients) and decouple the result from disk.
            with PILImage.open(local_path) as img:
                if scale and scale != 1.0:
                    img = img.resize(
                        (max(1, int(img.width * scale)),
                         max(1, int(img.height * scale))),
                        PILImage.Resampling.LANCZOS,
                    )
                buffer = BytesIO()
                img.save(buffer, format="PNG", optimize=True)
                return buffer.getvalue()
        finally:
            self._silent_rm_remote(remote)
            self._silent_rm_local(local_path)

    def get_uilayout(self) -> str:
        """Dump the current UI and return clickable elements with their centers.

        Hardened to use a unique device path and a private temp file instead of
        fixed filenames in the working directory.
        """
        token = uuid.uuid4().hex
        remote = f"/sdcard/mcp_uidump_{token}.xml"
        fd, local_path = tempfile.mkstemp(prefix="mcp_uidump_", suffix=".xml")
        os.close(fd)
        try:
            out = self.device.shell(f"uiautomator dump {remote}") or ""
            if "ERROR" in out and "dumped to" not in out:
                return f"Failed to dump UI hierarchy: {out.strip()}"
            self.device.pull(remote, local_path)
            return self._parse_uilayout(local_path)
        finally:
            self._silent_rm_remote(remote)
            self._silent_rm_local(local_path)

    @staticmethod
    def _parse_uilayout(xml_path: str) -> str:
        def calculate_center(bounds_str):
            matches = re.findall(r"\[(\d+),(\d+)\]", bounds_str)
            if len(matches) == 2:
                x1, y1 = map(int, matches[0])
                x2, y2 = map(int, matches[1])
                return (x1 + x2) // 2, (y1 + y2) // 2
            return None

        tree = ET.parse(xml_path)
        root = tree.getroot()

        clickable_elements = []
        for element in root.findall(".//node[@clickable='true']"):
            text = element.get("text", "")
            content_desc = element.get("content-desc", "")
            bounds = element.get("bounds", "")
            # Only include elements that have either text or content description
            if text or content_desc:
                center = calculate_center(bounds)
                element_info = "Clickable element:"
                if text:
                    element_info += f"\n  Text: {text}"
                if content_desc:
                    element_info += f"\n  Description: {content_desc}"
                element_info += f"\n  Bounds: {bounds}"
                if center:
                    element_info += f"\n  Center: ({center[0]}, {center[1]})"
                clickable_elements.append(element_info)

        if not clickable_elements:
            return "No clickable elements found with text or description"
        return "\n\n".join(clickable_elements)

    def _silent_rm_remote(self, remote_path: str) -> None:
        """Best-effort removal of a temp file on the device."""
        try:
            self.device.shell(f"rm -f {remote_path}")
        except Exception:
            pass

    @staticmethod
    def _silent_rm_local(local_path: str) -> None:
        """Best-effort removal of a local temp file."""
        try:
            os.remove(local_path)
        except OSError:
            pass

    # ---- UI interaction -------------------------------------------------

    @staticmethod
    def _shell_quote(value: str) -> str:
        """POSIX single-quote a string for the device shell."""
        return "'" + value.replace("'", "'\\''") + "'"

    def _escape_input_text(self, text: str) -> str:
        # `input text` maps %s to a space; do that first, then single-quote the
        # whole token so shell metacharacters pass through literally.
        return self._shell_quote(text.replace(" ", "%s"))

    _KEY_ALIASES = {
        "HOME": "KEYCODE_HOME", "BACK": "KEYCODE_BACK", "ENTER": "KEYCODE_ENTER",
        "MENU": "KEYCODE_MENU", "POWER": "KEYCODE_POWER",
        "RECENTS": "KEYCODE_APP_SWITCH", "APP_SWITCH": "KEYCODE_APP_SWITCH",
        "VOLUME_UP": "KEYCODE_VOLUME_UP", "VOLUME_DOWN": "KEYCODE_VOLUME_DOWN",
        "MUTE": "KEYCODE_VOLUME_MUTE", "TAB": "KEYCODE_TAB",
        "DEL": "KEYCODE_DEL", "DELETE": "KEYCODE_DEL", "BACKSPACE": "KEYCODE_DEL",
        "FORWARD_DEL": "KEYCODE_FORWARD_DEL", "SEARCH": "KEYCODE_SEARCH",
        "SPACE": "KEYCODE_SPACE", "ESC": "KEYCODE_ESCAPE", "ESCAPE": "KEYCODE_ESCAPE",
        "UP": "KEYCODE_DPAD_UP", "DOWN": "KEYCODE_DPAD_DOWN",
        "LEFT": "KEYCODE_DPAD_LEFT", "RIGHT": "KEYCODE_DPAD_RIGHT",
        "CENTER": "KEYCODE_DPAD_CENTER",
    }

    def _resolve_keycode(self, key) -> str:
        s = str(key).strip()
        if s.isdigit():
            return s
        upper = s.upper()
        if upper in self._KEY_ALIASES:
            return self._KEY_ALIASES[upper]
        return upper if upper.startswith("KEYCODE_") else f"KEYCODE_{upper}"

    def tap(self, x: int, y: int) -> str:
        self.device.shell(f"input tap {int(x)} {int(y)}")
        return f"Tapped ({int(x)}, {int(y)})"

    def long_press(self, x: int, y: int, duration_ms: int = 600) -> str:
        # A zero-distance swipe with a hold duration is a long press.
        self.device.shell(
            f"input swipe {int(x)} {int(y)} {int(x)} {int(y)} {int(duration_ms)}")
        return f"Long-pressed ({int(x)}, {int(y)}) for {int(duration_ms)}ms"

    def swipe(self, x1: int, y1: int, x2: int, y2: int,
              duration_ms: int = 300) -> str:
        self.device.shell(
            f"input swipe {int(x1)} {int(y1)} {int(x2)} {int(y2)} {int(duration_ms)}")
        return (f"Swiped ({int(x1)},{int(y1)}) -> ({int(x2)},{int(y2)}) "
                f"over {int(duration_ms)}ms")

    def input_text(self, text: str) -> str:
        self.device.shell(f"input text {self._escape_input_text(text)}")
        return f"Typed {len(text)} character(s)"

    def press_key(self, key) -> str:
        keycode = self._resolve_keycode(key)
        self.device.shell(f"input keyevent {keycode}")
        return f"Pressed {keycode}"

    def launch_app(self, package: str) -> str:
        out = self.device.shell(
            f"cmd package resolve-activity --brief {package}") or ""
        component = None
        for line in out.splitlines():
            line = line.strip()
            if "/" in line and " " not in line:
                component = line
        if not component:
            self.device.shell(
                f"monkey -p {package} -c android.intent.category.LAUNCHER 1")
            return f"Launched {package} (monkey fallback; could not resolve activity)"
        result = self.device.shell(f"am start -n {component}") or ""
        if "Error" in result or "does not exist" in result:
            return f"Failed to launch {package}: {result.strip()}"
        return f"Launched {package} ({component})"

    # ---- device state / lifecycle --------------------------------------

    def get_current_app(self) -> str:
        out = self.device.shell("dumpsys activity activities") or ""
        for pattern in (
            r"mResumedActivity:[^\n]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)",
            r"ResumedActivity:[^\n]*?\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)",
        ):
            m = re.search(pattern, out)
            if m:
                return f"{m.group(1)}/{m.group(2)}"
        out2 = self.device.shell("dumpsys window") or ""
        m = re.search(
            r"mCurrentFocus=Window\{[^}]*\s([A-Za-z0-9_.]+)/([A-Za-z0-9_.$]+)", out2)
        if m:
            return f"{m.group(1)}/{m.group(2)}"
        return "Unable to determine the foreground app."

    def device_info(self) -> str:
        props = [
            ("manufacturer", "ro.product.manufacturer"),
            ("model", "ro.product.model"),
            ("android_version", "ro.build.version.release"),
            ("sdk", "ro.build.version.sdk"),
            ("build", "ro.build.display.id"),
            ("abi", "ro.product.cpu.abi"),
        ]
        lines = [f"{label}: {(self.device.shell(f'getprop {prop}') or '').strip()}"
                 for label, prop in props]
        lines.append((self.device.shell("wm size") or "").strip())
        lines.append((self.device.shell("wm density") or "").strip())
        battery = self.device.shell("dumpsys battery") or ""
        m = re.search(r"level: (\d+)", battery)
        if m:
            lines.append(f"battery level: {m.group(1)}")
        return "\n".join(line for line in lines if line)

    def install_apk(self, apk_path: str, reinstall: bool = True) -> str:
        if not os.path.exists(apk_path):
            raise RuntimeError(f"APK not found on the server host: {apk_path}")
        try:
            self.device.install(apk_path, reinstall=reinstall)
        except Exception as e:
            raise RuntimeError(f"Install failed: {e}")
        return f"Installed {os.path.basename(apk_path)}"

    def uninstall_app(self, package: str) -> str:
        out = (self.device.shell(f"pm uninstall {package}") or "").strip()
        if "Success" in out:
            return f"Uninstalled {package}"
        return f"Uninstall result for {package}: {out or 'no output'}"

    def force_stop(self, package: str) -> str:
        self.device.shell(f"am force-stop {package}")
        return f"Force-stopped {package}"

    def get_clipboard(self) -> str:
        """Read the device clipboard (Android 13+ `cmd clipboard`)."""
        out = (self.device.shell("cmd clipboard get-text") or "").strip()
        return out if out else "(clipboard empty or unavailable)"

    def set_clipboard(self, text: str) -> str:
        """Set the device clipboard (Android 13+ `cmd clipboard`)."""
        self.device.shell(f"cmd clipboard set-text {self._shell_quote(text)}")
        return "Clipboard set."
