import os
import sys

import yaml
from mcp.server.fastmcp import FastMCP, Image

from adbdevicemanager import AdbDeviceManager
from ssh_tunnel import SshTunnel

CONFIG_FILE = "config.yaml"
CONFIG_FILE_EXAMPLE = "config.yaml.example"
DEFAULT_ADB = ("127.0.0.1", 5037)


def _log(message: str) -> None:
    """Log to stderr.

    The stdio MCP transport uses stdout for the JSON-RPC protocol stream, so
    any diagnostic output must go to stderr or it will corrupt the handshake.
    """
    print(message, file=sys.stderr)


def _read_config() -> dict:
    """Read config.yaml into a dict (empty dict if missing/blank)."""
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE) as f:
        return yaml.safe_load(f.read()) or {}


def _write_config(config: dict) -> None:
    """Persist the config dict back to config.yaml (runtime state, gitignored)."""
    with open(CONFIG_FILE, "w") as f:
        yaml.safe_dump(config, f, default_flow_style=False, sort_keys=False)


def load_config() -> tuple[str | None, dict | None]:
    """Load the configured device name and SSH connection from config.yaml.

    Returns (device_name, ssh_config). Either may be None. Never aborts the
    process for a missing/unspecified device — that is resolved lazily when a
    device-dependent tool is first called.
    """
    if not os.path.exists(CONFIG_FILE):
        _log(f"Config file {CONFIG_FILE} not found, using auto-selection for device")
        return None, None

    try:
        config = _read_config()
    except Exception as e:
        _log(f"Error loading config file {CONFIG_FILE}: {e}")
        _log(f"Please check the format of your config file or recreate it from {CONFIG_FILE_EXAMPLE}")
        sys.exit(1)

    device_config = config.get("device", {})
    configured_device_name = device_config.get("name") if device_config else None

    # Support multiple ways to specify auto-selection:
    # 1. name: null (None in Python)
    # 2. name: "" (empty string)
    # 3. name field completely missing
    if configured_device_name and configured_device_name.strip():
        device_name = configured_device_name.strip()
        _log(f"Loaded config from {CONFIG_FILE}")
        _log(f"Configured device: {device_name}")
    else:
        device_name = None
        _log(f"Loaded config from {CONFIG_FILE}")
        _log("No device specified in config, will auto-select if only one device connected")

    ssh_config = config.get("ssh") or None
    if ssh_config:
        _log(f"SSH connection persisted for {ssh_config.get('host')}, "
             "will reconnect on first command")

    return device_name, ssh_config


# Initialize MCP. The device manager and any SSH tunnel are created lazily so
# the server always starts — even with no device connected, the wrong device,
# adb missing, or an unreachable SSH host. Those conditions surface as per-call
# errors instead of taking the whole server down at startup.
mcp = FastMCP("android")
device_name, _persisted_ssh = load_config()
_adb_endpoint: tuple[str, int] = DEFAULT_ADB
_ssh_tunnel: SshTunnel | None = None
_device_manager: AdbDeviceManager | None = None


def _ssh_tunnel_from_config(cfg: dict) -> SshTunnel:
    return SshTunnel(
        host=cfg["host"],
        user=cfg.get("user"),
        port=cfg.get("port", 22),
        key_path=cfg.get("key_path"),
        remote_adb_port=cfg.get("remote_adb_port", 5037),
        strict_host_key=cfg.get("strict_host_key", "accept-new"),
    )


def _ensure_ssh() -> None:
    """Lazily (re)establish the persisted SSH tunnel if one is configured.

    Called before any adb access. A no-op when no SSH connection is configured
    or the tunnel is already live. Raises (surfaced as a tool error) if a
    configured tunnel cannot be established.
    """
    global _ssh_tunnel, _adb_endpoint, _device_manager, device_name
    if not _persisted_ssh:
        return
    if _ssh_tunnel is not None and _ssh_tunnel.is_alive():
        return

    tunnel = _ssh_tunnel_from_config(_persisted_ssh)
    local_port = tunnel.start()
    _ssh_tunnel = tunnel
    _adb_endpoint = ("127.0.0.1", local_port)
    device_name = _persisted_ssh.get("device") or None
    _device_manager = None  # rebuild against the tunnelled endpoint
    _log(f"SSH tunnel up: {tunnel.target} (remote adb -> 127.0.0.1:{local_port})")


def _teardown_ssh() -> None:
    global _ssh_tunnel, _adb_endpoint, _device_manager
    if _ssh_tunnel is not None:
        _ssh_tunnel.stop()
    _ssh_tunnel = None
    _adb_endpoint = DEFAULT_ADB
    _device_manager = None


def get_device_manager() -> AdbDeviceManager:
    """Return a connected device manager, creating it on first use.

    Establishes the persisted SSH tunnel first if one is configured. Raises
    RuntimeError (surfaced to the MCP client as a tool error) when no device can
    be selected. A failed attempt is not cached, so the next call retries.
    """
    global _device_manager
    _ensure_ssh()
    if _device_manager is None:
        host, port = _adb_endpoint
        _device_manager = AdbDeviceManager(
            device_name, exit_on_error=False, host=host, port=port)
    return _device_manager


@mcp.tool()
def list_devices() -> str:
    """List the serials of all currently connected ADB devices.

    Works even when no device is selected. If an SSH connection is configured,
    this lists the devices on the remote host. Use it to discover devices and,
    if more than one is connected, set the desired serial via connect_ssh or
    config.yaml.

    Returns:
        str: One device serial per line, or a message if none are connected.
    """
    try:
        _ensure_ssh()
        host, port = _adb_endpoint
        devices = AdbDeviceManager.get_available_devices(host, port)
    except Exception as e:
        return f"Unable to query devices: {e}"
    if not devices:
        return "No devices connected."
    return "\n".join(devices)


@mcp.tool()
def connect_ssh(host: str, user: str = "", port: int = 22, key_path: str = "",
                remote_adb_port: int = 5037, device: str = "",
                strict_host_key: str = "accept-new", persist: bool = True) -> str:
    """Open a persistent SSH tunnel to a remote host's adb server and drive its devices.

    Forwards the remote machine's adb server port over SSH to a local port and
    points all subsequent adb tools (shell, screenshot, UI dump, packages) at
    the remote device. The tunnel stays up for the life of the server, and when
    persist is true it is saved to config.yaml and re-established automatically
    after a restart. Key-based auth only (no password prompts).

    Args:
        host: SSH host of the remote machine running adb (and the device/emulator).
        user: SSH username (optional if your ssh config supplies it).
        port: SSH port (default 22).
        key_path: Path to a private key file (optional; uses your ssh agent/config otherwise).
        remote_adb_port: The adb server port on the remote host (default 5037).
        device: Optional device serial to pre-select on the remote (e.g. "emulator-5554").
        strict_host_key: ssh StrictHostKeyChecking value (default "accept-new").
        persist: Save the connection to config.yaml so it survives a restart (default true).

    Returns:
        str: Connection status and the devices visible on the remote host.
    """
    global _ssh_tunnel, _adb_endpoint, _device_manager, device_name, _persisted_ssh

    cfg = {
        "host": host,
        "user": user or None,
        "port": port,
        "key_path": key_path or None,
        "remote_adb_port": remote_adb_port,
        "strict_host_key": strict_host_key,
        "device": device or None,
    }

    _teardown_ssh()
    try:
        tunnel = _ssh_tunnel_from_config(cfg)
        local_port = tunnel.start()
    except Exception as e:
        return f"Failed to open SSH tunnel to {cfg['host']}: {e}"

    _ssh_tunnel = tunnel
    _adb_endpoint = ("127.0.0.1", local_port)
    device_name = device or None
    _device_manager = None
    _persisted_ssh = cfg

    if persist:
        try:
            full = _read_config()
            full["ssh"] = {k: v for k, v in cfg.items() if v is not None}
            _write_config(full)
        except Exception as e:
            _log(f"Warning: could not persist SSH config: {e}")

    try:
        devices = AdbDeviceManager.get_available_devices(*_adb_endpoint)
    except Exception as e:
        devices = []
        _log(f"Connected, but failed to list remote devices: {e}")

    lines = [
        f"Connected to {tunnel.target} "
        f"(remote adb :{remote_adb_port} -> 127.0.0.1:{local_port}).",
        "Remote devices:",
        ("\n".join(f"  {d}" for d in devices) if devices else "  (none visible)"),
    ]
    return "\n".join(lines)


@mcp.tool()
def disconnect_ssh() -> str:
    """Tear down the SSH tunnel and revert to the local adb server.

    Also removes the persisted SSH connection so it is not re-established on the
    next command or restart.

    Returns:
        str: Confirmation message.
    """
    global _persisted_ssh
    _teardown_ssh()
    _persisted_ssh = None
    try:
        full = _read_config()
        if "ssh" in full:
            del full["ssh"]
            _write_config(full)
    except Exception as e:
        _log(f"Warning: could not clear persisted SSH config: {e}")
    return "Disconnected SSH tunnel; now using the local adb server."


@mcp.tool()
def ssh_status() -> str:
    """Report the current SSH tunnel / adb endpoint state.

    Returns:
        str: A human-readable description of the active connection.
    """
    if _ssh_tunnel is not None and _ssh_tunnel.is_alive():
        return (f"SSH tunnel active: {_ssh_tunnel.target} "
                f"(remote adb -> 127.0.0.1:{_ssh_tunnel.local_port}).")
    if _persisted_ssh:
        return (f"SSH configured for {_persisted_ssh.get('host')} but not connected; "
                "it will be re-established on the next command.")
    return "No SSH tunnel; using the local adb server (127.0.0.1:5037)."


@mcp.tool()
def get_packages() -> str:
    """
    Get all installed packages on the device
    Returns:
        str: A list of all installed packages on the device as a string
    """
    result = get_device_manager().get_packages()
    return result


@mcp.tool()
def execute_adb_shell_command(command: str) -> str:
    """Executes an ADB command and returns the output or an error.
    Args:
        command (str): The ADB shell command to execute
    Returns:
        str: The output of the ADB command
    """
    result = get_device_manager().execute_adb_shell_command(command)
    return result


@mcp.tool()
def get_uilayout() -> str:
    """
    Retrieves information about clickable elements in the current UI.
    Returns a formatted string containing details about each clickable element,
    including its text, content description, bounds, and center coordinates.

    Returns:
        str: A formatted list of clickable elements with their properties
    """
    result = get_device_manager().get_uilayout()
    return result


@mcp.tool()
def get_screenshot() -> Image:
    """Takes a screenshot of the device and returns it as a PNG image.
    Returns:
        Image: the screenshot
    """
    data = get_device_manager().take_screenshot()
    return Image(data=data, format="png")


@mcp.tool()
def get_package_action_intents(package_name: str) -> list[str]:
    """
    Get all non-data actions from Activity Resolver Table for a package
    Args:
        package_name (str): The name of the package to get actions for
    Returns:
        list[str]: A list of all non-data actions from the Activity Resolver Table for the package
    """
    result = get_device_manager().get_package_action_intents(package_name)
    return result


# ---- UI interaction ----------------------------------------------------


@mcp.tool()
def tap(x: int, y: int) -> str:
    """Tap (single click) at a screen coordinate.

    Args:
        x: X pixel coordinate.
        y: Y pixel coordinate.
    Returns:
        str: Confirmation of the tap.
    """
    return get_device_manager().tap(x, y)


@mcp.tool()
def long_press(x: int, y: int, duration_ms: int = 600) -> str:
    """Long-press (touch and hold) at a screen coordinate.

    Args:
        x: X pixel coordinate.
        y: Y pixel coordinate.
        duration_ms: Hold duration in milliseconds (default 600).
    Returns:
        str: Confirmation of the long press.
    """
    return get_device_manager().long_press(x, y, duration_ms)


@mcp.tool()
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Swipe/drag from one coordinate to another (also used to scroll).

    Args:
        x1: Start X. y1: Start Y. x2: End X. y2: End Y.
        duration_ms: Gesture duration in milliseconds (default 300).
    Returns:
        str: Confirmation of the swipe.
    """
    return get_device_manager().swipe(x1, y1, x2, y2, duration_ms)


@mcp.tool()
def input_text(text: str) -> str:
    """Type text into the currently focused input field.

    Tap the field first so it has focus. Spaces and shell metacharacters are
    handled for you.

    Args:
        text: The text to type.
    Returns:
        str: Confirmation.
    """
    return get_device_manager().input_text(text)


@mcp.tool()
def press_key(key: str) -> str:
    """Send a key event.

    Accepts a numeric Android keycode, a full name (e.g. "KEYCODE_ENTER"), or a
    friendly alias: HOME, BACK, ENTER, MENU, POWER, RECENTS, VOLUME_UP,
    VOLUME_DOWN, TAB, DEL/BACKSPACE, SEARCH, SPACE, ESC, UP, DOWN, LEFT, RIGHT,
    CENTER.

    Args:
        key: Keycode, name, or alias.
    Returns:
        str: Confirmation.
    """
    return get_device_manager().press_key(key)


@mcp.tool()
def go_home() -> str:
    """Press the HOME button.

    Returns:
        str: Confirmation.
    """
    return get_device_manager().press_key("HOME")


@mcp.tool()
def go_back() -> str:
    """Press the BACK button.

    Returns:
        str: Confirmation.
    """
    return get_device_manager().press_key("BACK")


@mcp.tool()
def launch_app(package: str) -> str:
    """Launch an app by package name via its launcher activity.

    Resolves the launcher activity and uses `am start` (deterministic), falling
    back to monkey only if resolution fails.

    Args:
        package: The app package name, e.g. "com.android.settings".
    Returns:
        str: The launched component, or an error message.
    """
    return get_device_manager().launch_app(package)


# ---- device state / lifecycle ------------------------------------------


@mcp.tool()
def get_current_app() -> str:
    """Report the foreground app as "package/activity".

    Returns:
        str: The resumed package/activity, or a message if it can't be found.
    """
    return get_device_manager().get_current_app()


@mcp.tool()
def device_info() -> str:
    """Summarize the device: model, Android version, SDK, ABI, screen size/density, battery.

    Returns:
        str: One property per line.
    """
    return get_device_manager().device_info()


@mcp.tool()
def install_apk(apk_path: str, reinstall: bool = True) -> str:
    """Install an APK located on the machine running this server.

    The path is read on the server host (works through the SSH tunnel too).

    Args:
        apk_path: Absolute path to a .apk on the server host.
        reinstall: Keep existing data and reinstall (-r) if already installed.
    Returns:
        str: Confirmation; raises with the install error on failure.
    """
    return get_device_manager().install_apk(apk_path, reinstall)


@mcp.tool()
def uninstall_app(package: str) -> str:
    """Uninstall an app by package name.

    Args:
        package: The app package name.
    Returns:
        str: The uninstall result.
    """
    return get_device_manager().uninstall_app(package)


@mcp.tool()
def force_stop(package: str) -> str:
    """Force-stop a running app.

    Args:
        package: The app package name.
    Returns:
        str: Confirmation.
    """
    return get_device_manager().force_stop(package)


@mcp.tool()
def get_clipboard() -> str:
    """Read the device clipboard text (requires Android 13+).

    Returns:
        str: Clipboard contents, or a note if empty/unavailable.
    """
    return get_device_manager().get_clipboard()


@mcp.tool()
def set_clipboard(text: str) -> str:
    """Set the device clipboard text (requires Android 13+).

    Args:
        text: The text to place on the clipboard.
    Returns:
        str: Confirmation.
    """
    return get_device_manager().set_clipboard(text)


if __name__ == "__main__":
    mcp.run(transport="stdio")
