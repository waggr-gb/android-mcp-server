# Android MCP Server

An MCP (Model Context Protocol) server that provides programmatic control over
Android devices through ADB (Android Debug Bridge). This server exposes
various Android device management capabilities that can be accessed by MCP
clients like [Claude desktop](https://modelcontextprotocol.io/quickstart/user)
and Code editors
(e.g. [Cursor](https://docs.cursor.com/context/model-context-protocol))

## Features

- 🔧 ADB Command Execution
- 📸 Device Screenshot Capture
- 🎯 UI Layout Analysis
- 👆 UI Interaction (`tap`, `swipe`, `input_text`, `press_key`, `launch_app`, ...)
- 📦 App Lifecycle (`install_apk`, `uninstall_app`, `force_stop`, `get_current_app`, `device_info`)
- 📱 Device Package Management
- 🔌 Device Discovery (`list_devices`)
- 🛰️ Remote devices over SSH (`connect_ssh`) — persists across restarts

The server always starts even when no device is connected: device selection is
lazy, so "no device", "wrong device", and "adb missing" surface as per-call
errors instead of killing the server at startup.

## Prerequisites

- Python 3.x
- ADB (Android Debug Bridge) installed and configured
- Android device or emulator (not tested)

## Installation

1. Clone the repository:

```bash
git clone https://github.com/minhalvp/android-mcp-server.git
cd android-mcp-server
```

2. Install dependencies:
This project uses [uv](https://github.com/astral-sh/uv) for project
management via various methods of
[installation](https://docs.astral.sh/uv/getting-started/installation/).

```bash
uv python install 3.11
uv sync
```

## Configuration

The server supports flexible device configuration with multiple usage scenarios.

### Device Selection Modes

**1. Automatic Selection (Recommended for single device)**

- No configuration file needed
- Automatically connects to the only connected device
- Perfect for development with a single test device

**2. Manual Device Selection**

- Use when you have multiple devices connected
- Specify exact device in configuration file

### Configuration File (Optional)

The configuration file (`config.yaml`) is **optional**. If not present, the server will automatically select the device if only one is connected.

#### For Automatic Selection

Simply ensure only one device is connected and run the server - no configuration needed!

#### For Manual Selection

1. Create a configuration file:

```bash
cp config.yaml.example config.yaml
```

2. Edit `config.yaml` and specify your device:

```yaml
device:
  name: "your-device-serial-here" # Device identifier from 'adb devices'
```

**For auto-selection**, you can use any of these methods:

```yaml
device:
  name: null              # Explicit null (recommended)
  # name: ""              # Empty string  
  # name:                 # Or leave empty/comment out
```

### Finding Your Device Serial

To find your device identifier, run:

```bash
adb devices
```

Example output:

```
List of devices attached
13b22d7f        device
emulator-5554   device
```

Use the first column value (e.g., `13b22d7f` or `emulator-5554`) as the device name.

### Usage Scenarios

| Scenario | Configuration Required | Behavior |
|----------|----------------------|----------|
| Single device connected | None | ✅ Auto-connects to the device |
| Multiple devices, want specific one | `config.yaml` with `device.name` | ✅ Connects to specified device |
| Multiple devices, no config | None | ❌ Shows error with available devices |
| No devices connected | N/A | ❌ Shows "no devices" error |

**Note**: If you have multiple devices connected and don't specify which one to use, the server will show an error message listing all available devices.

## Usage

An MCP client is needed to use this server. The Claude Desktop app is an example
of an MCP client. To use this server with Claude Desktop:

1. Locate your Claude Desktop configuration file:

   - Windows: `%APPDATA%\Claude\claude_desktop_config.json`
   - macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`

2. Add the Android MCP server configuration to the `mcpServers` section:

```json
{
  "mcpServers": {
    "android": {
      "command": "path/to/uv",
      "args": ["--directory", "path/to/android-mcp-server", "run", "server.py"]
    }
  }
}
```

Replace:

- `path/to/uv` with the actual path to your `uv` executable
- `path/to/android-mcp-server` with the absolute path to where you cloned this
repository

<https://github.com/user-attachments/assets/c45bbc17-f698-43e7-85b4-f1b39b8326a8>

## Remote devices over SSH

To drive a device or emulator that lives on another machine (e.g. a CI runner
provisioning a per-PR emulator that is only reachable over SSH), use the
`connect_ssh` tool. It opens a persistent SSH local port-forward from the remote
host's adb server (port `5037`) to a local port and points all adb tools at it,
so `execute_adb_shell_command`, `get_screenshot`, `get_uilayout`, etc. all
transparently target the remote device.

Typical flow:

1. `list_devices` — see what's connected locally (or, once connected, remotely).
2. `connect_ssh(host="runner1", user="ci", device="emulator-5554")` — open the
   tunnel and select the remote device.
3. Any other command (`execute_adb_shell_command`, `get_screenshot`, ...) now
   runs against the remote device.
4. `ssh_status` to check state, `disconnect_ssh` to revert to local adb.

The tunnel is held open by the (long-lived) MCP server, so it persists across
tool calls. With `persist: true` (the default) the connection is also saved to
`config.yaml` and re-established automatically after a restart. Authentication
is **key-based only** (no password prompts) — use your ssh agent/config or pass
`key_path`.

### Available Tools

The server exposes the following tools:

```python
def list_devices() -> str:
    """
    List the serials of all currently connected ADB devices (works with zero,
    one, or many devices; lists remote devices when connected over SSH).
    """
```

```python
def connect_ssh(host: str, user: str = "", port: int = 22, key_path: str = "",
                remote_adb_port: int = 5037, device: str = "",
                strict_host_key: str = "accept-new", persist: bool = True) -> str:
    """
    Open a persistent SSH tunnel to a remote host's adb server and drive its
    devices. Subsequent adb tools target the remote device. Persists to
    config.yaml by default so it survives a restart.
    """

def disconnect_ssh() -> str:
    """Tear down the SSH tunnel and revert to the local adb server."""

def ssh_status() -> str:
    """Report the current SSH tunnel / adb endpoint state."""
```

```python
def get_packages() -> str:
    """
    Get all installed packages on the device.
    Returns:
        str: A list of all installed packages on the device as a string
    """
```

```python
def execute_adb_command(command: str) -> str:
    """
    Executes an ADB command and returns the output.
    Args:
        command (str): The ADB command to execute
    Returns:
        str: The output of the ADB command
    """
```

```python
def get_uilayout() -> str:
    """
    Retrieves information about clickable elements in the current UI.
    Returns a formatted string containing details about each clickable element,
    including their text, content description, bounds, and center coordinates.

    Returns:
        str: A formatted list of clickable elements with their properties
    """
```

```python
def get_screenshot() -> Image:
    """
    Takes a screenshot of the device and returns it.
    Returns:
        Image: the screenshot
    """
```

```python
def get_package_action_intents(package_name: str) -> list[str]:
    """
    Get all non-data actions from Activity Resolver Table for a package
    Args:
        package_name (str): The name of the package to get actions for
    Returns:
        list[str]: A list of all non-data actions from the Activity Resolver
        Table for the package
    """
```

**UI interaction** — drive the device the way a user would (pair with
`get_uilayout` / `get_screenshot` to find coordinates):

```python
def tap(x: int, y: int) -> str: ...
def long_press(x: int, y: int, duration_ms: int = 600) -> str: ...
def swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300) -> str:
    """Swipe/drag between two points (also used to scroll)."""
def input_text(text: str) -> str:
    """Type into the focused field (spaces/metacharacters handled for you)."""
def press_key(key: str) -> str:
    """Numeric keycode, KEYCODE_* name, or alias (HOME, BACK, ENTER, MENU,
    RECENTS, VOLUME_UP/DOWN, TAB, DEL, SEARCH, ESC, UP/DOWN/LEFT/RIGHT, ...)."""
def go_home() -> str: ...
def go_back() -> str: ...
def launch_app(package: str) -> str:
    """Launch an app via its resolved launcher activity (am start)."""
```

**Device state & lifecycle:**

```python
def get_current_app() -> str:
    """The foreground app as "package/activity"."""
def device_info() -> str:
    """Model, Android version, SDK, ABI, screen size/density, battery level."""
def install_apk(apk_path: str, reinstall: bool = True) -> str:
    """Install an APK from the server host (works through the SSH tunnel)."""
def uninstall_app(package: str) -> str: ...
def force_stop(package: str) -> str: ...
def get_clipboard() -> str:  # Android 13+
    ...
def set_clipboard(text: str) -> str:  # Android 13+
    ...
```

## Contributing

Contributions are welcome!

## Acknowledgments

- Built with
[Model Context Protocol (MCP)](https://modelcontextprotocol.io/introduction)
