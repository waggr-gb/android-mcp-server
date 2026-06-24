"""
Persistent SSH local port-forward to a remote host's adb server.

The MCP server runs ppadb against a local adb server (127.0.0.1:5037). To drive
a device that lives on a remote machine (e.g. a CI runner provisioning a per-PR
emulator that is only reachable over SSH), we forward the remote machine's adb
server port to a local ephemeral port and point ppadb at it. Every adb operation
(shell, screenshot pull, uiautomator dump) then transparently targets the remote
device.

The tunnel is a background `ssh -N -L` process owned by the long-lived MCP
server, so it persists across tool calls for the lifetime of the server.
"""

import socket
import subprocess
import time


def _find_free_port() -> int:
    """Ask the OS for a free local TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _port_open(port: int, timeout: float = 0.5) -> bool:
    """Return True if something accepts a TCP connection on 127.0.0.1:port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex(("127.0.0.1", port)) == 0


class SshTunnel:
    """A persistent SSH local port-forward to a remote adb server.

    Forwards 127.0.0.1:<local_port> -> <remote_bind>:<remote_adb_port> on the
    SSH host. Key-based auth only (BatchMode) so it never blocks on a prompt.
    """

    def __init__(self, host: str, user: str | None = None, port: int = 22,
                 key_path: str | None = None, remote_adb_port: int = 5037,
                 remote_bind: str = "127.0.0.1",
                 strict_host_key: str = "accept-new", connect_timeout: int = 10,
                 extra_opts: list[str] | None = None) -> None:
        self.host = host
        self.user = user or None
        self.port = int(port)
        self.key_path = key_path or None
        self.remote_adb_port = int(remote_adb_port)
        self.remote_bind = remote_bind
        self.strict_host_key = strict_host_key
        self.connect_timeout = int(connect_timeout)
        self.extra_opts = list(extra_opts or [])
        self.local_port: int | None = None
        self._proc: subprocess.Popen | None = None

    @property
    def target(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def build_command(self, local_port: int) -> list[str]:
        forward = f"{local_port}:{self.remote_bind}:{self.remote_adb_port}"
        cmd = [
            "ssh", "-N", "-T",
            "-o", "BatchMode=yes",
            "-o", f"StrictHostKeyChecking={self.strict_host_key}",
            "-o", f"ConnectTimeout={self.connect_timeout}",
            "-o", "ExitOnForwardFailure=yes",
            "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3",
            "-p", str(self.port),
            "-L", forward,
        ]
        if self.key_path:
            cmd += ["-i", self.key_path, "-o", "IdentitiesOnly=yes"]
        cmd += self.extra_opts
        cmd.append(self.target)
        return cmd

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def start(self, wait_timeout: int = 15) -> int:
        """Start the tunnel and block until the local port is forwarding.

        Returns the local port. Raises RuntimeError if ssh exits or the forward
        does not come up within wait_timeout seconds.
        """
        if self.is_alive():
            return self.local_port  # type: ignore[return-value]

        local_port = _find_free_port()
        self._proc = subprocess.Popen(
            self.build_command(local_port),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        deadline = time.monotonic() + wait_timeout
        while time.monotonic() < deadline:
            if self._proc.poll() is not None:
                out = self._proc.stdout.read() if self._proc.stdout else ""
                self._proc = None
                raise RuntimeError(
                    f"SSH tunnel to {self.target} failed: {out.strip() or 'ssh exited'}")
            if _port_open(local_port):
                self.local_port = local_port
                return local_port
            time.sleep(0.25)

        self.stop()
        raise RuntimeError(
            f"SSH tunnel to {self.target} did not become ready within {wait_timeout}s")

    def stop(self) -> None:
        """Tear down the tunnel process."""
        if self._proc is not None:
            try:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
            finally:
                self._proc = None
        self.local_port = None
