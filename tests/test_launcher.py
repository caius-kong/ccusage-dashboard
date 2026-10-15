#!/usr/bin/env python3
"""Regression tests for daemon reuse across package upgrades."""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "lib"))
import server as dashboard_server  # noqa: E402


class HealthHandler(BaseHTTPRequestHandler):
    version = "0.2.22"

    def do_GET(self):  # noqa: N802
        if self.path != "/api/health":
            self.send_error(404)
            return
        body = json.dumps({"ok": True, "version": self.version}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        pass


class LauncherTest(unittest.TestCase):
    def launch_against(self, version: str | None) -> str:
        HealthHandler.version = version
        server = ThreadingHTTPServer(("127.0.0.1", 0), HealthHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            result = subprocess.run(
                [
                    "node",
                    str(ROOT / "bin" / "ccusage-ui.js"),
                    "--port",
                    str(server.server_port),
                    "--no-update-check",
                ],
                capture_output=True,
                text=True,
                timeout=10,
            )
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout + result.stderr

    def test_new_launcher_replaces_older_managed_server(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        pid_path = Path(tempfile.gettempdir()) / f"ccusage-dashboard-127_0_0_1-{port}.pid"
        with tempfile.TemporaryDirectory() as temp_dir:
            old_script = Path(temp_dir) / "ccusage-dashboard" / "lib" / "server.py"
            old_script.parent.mkdir(parents=True)
            old_script.write_text(
                "from http.server import BaseHTTPRequestHandler, HTTPServer\n"
                "class H(BaseHTTPRequestHandler):\n"
                " def do_GET(self):\n"
                "  body=b'{\\\"ok\\\":true,\\\"version\\\":\\\"0.2.22\\\"}'\n"
                "  self.send_response(200); self.send_header('Content-Length',str(len(body))); self.end_headers(); self.wfile.write(body)\n"
                " def log_message(self,*args): pass\n"
                f"HTTPServer(('127.0.0.1',{port}),H).serve_forever()\n",
                encoding="utf-8",
            )
            old_server = subprocess.Popen(
                [sys.executable, str(old_script), "--port", str(port)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            env = {**os.environ, "CCUSAGE_NO_UPDATE_CHECK": "1"}
            try:
                deadline = time.monotonic() + 5
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2):
                            break
                    except Exception:
                        time.sleep(0.05)
                result = subprocess.run(
                    ["node", str(ROOT / "bin" / "ccusage-ui.js"), "--port", str(port), "--no-update-check", "--no-warm"],
                    capture_output=True,
                    text=True,
                    timeout=15,
                    env=env,
                )
                deadline = time.monotonic() + 5
                version = None
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=0.2) as response:
                            version = json.load(response).get("version")
                        if version == dashboard_server._read_current_version():
                            break
                    except Exception:
                        pass
                    time.sleep(0.05)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("Stopping older dashboard v0.2.22", result.stdout)
                self.assertEqual(version, dashboard_server._read_current_version())
            finally:
                try:
                    new_pid = int(pid_path.read_text(encoding="utf-8"))
                    os.kill(new_pid, 15)
                except (OSError, ValueError):
                    pass
                try:
                    old_server.terminate()
                    old_server.wait(timeout=2)
                except (OSError, subprocess.TimeoutExpired):
                    pass
                pid_path.unlink(missing_ok=True)

    def test_versionless_server_is_not_killed_without_pid_ownership(self) -> None:
        output = self.launch_against(None)

        self.assertIn("cannot be safely replaced automatically", output)
        self.assertIn("Stop the existing server manually", output)

    def test_health_endpoint_reports_running_package_version(self) -> None:
        server = ThreadingHTTPServer(("127.0.0.1", 0), dashboard_server.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{server.server_port}/api/health") as response:
                health = json.load(response)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        package = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        self.assertEqual(health["version"], package["version"])


if __name__ == "__main__":
    unittest.main()
