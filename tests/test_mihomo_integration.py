"""Local end-to-end test; requires run/mihomo/bin/mihomo.exe (or MIHOMO_TEST_CORE)."""
import json
import os
import shutil
import socket
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import psutil
import requests
import yaml

from core.mihomo_proxy import ROOT, MihomoManager, ProxyError

CORE = Path(os.environ.get("MIHOMO_TEST_CORE", ROOT / "run" / "mihomo" / "bin" / ("mihomo.exe" if os.name == "nt" else "mihomo")))


def serve(handler):
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server


def free_ports():
    with socket.socket() as first, socket.socket() as second:
        first.bind(("127.0.0.1", 0))
        second.bind(("127.0.0.1", 0))
        return first.getsockname()[1], second.getsockname()[1]


def proxy_handler(label):
    class Proxy(BaseHTTPRequestHandler):
        def do_CONNECT(self):
            self.send_response(200)
            self.end_headers()
            self.connection.settimeout(5)
            request_line = self.rfile.readline(8192).decode("ascii")
            while self.rfile.readline(8192) not in (b"\r\n", b"\n", b""):
                pass
            data = json.dumps({"node": label, "destination": request_line}).encode()
            self.wfile.write(f"HTTP/1.1 200 OK\r\nContent-Length: {len(data)}\r\nConnection: close\r\n\r\n".encode() + data)
            self.wfile.flush()
            self.close_connection = True

        def do_GET(self):
            data = json.dumps({"node": label, "destination": self.path}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args):
            pass
    return Proxy


@unittest.skipUnless(CORE.is_file(), "Mihomo core not installed")
class MihomoIntegrationTests(unittest.TestCase):
    def test_real_core_import_select_restart_refresh_and_stop(self):
        external = {(p.pid, p.create_time()) for p in psutil.process_iter(["name"]) if p.info["name"] and "mihomo" in p.info["name"].lower()}
        server_a = serve(proxy_handler("node-a"))
        server_b = serve(proxy_handler("node-b"))
        self.addCleanup(server_a.server_close)
        self.addCleanup(server_a.shutdown)
        self.addCleanup(server_b.server_close)
        self.addCleanup(server_b.shutdown)
        payload = {"mixed-port": 7897, "tun": {"enable": True}, "proxies": [
            {"name": "Local A", "type": "http", "server": "127.0.0.1", "port": server_a.server_port},
            {"name": "Local B", "type": "http", "server": "127.0.0.1", "port": server_b.server_port},
        ]}

        class Subscription(BaseHTTPRequestHandler):
            def do_GET(self):
                data = yaml.safe_dump(payload).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        subscription = serve(Subscription)
        self.addCleanup(subscription.server_close)
        self.addCleanup(subscription.shutdown)
        with tempfile.TemporaryDirectory(prefix="mihomo-test-") as directory:
            manager = MihomoManager(Path(directory), busy_check=lambda: False)
            manager.core_path.parent.mkdir(parents=True)
            shutil.copy2(CORE, manager.core_path)
            mixed, control = free_ports()
            manager.configure_ports(mixed, control)
            url = f"http://127.0.0.1:{subscription.server_port}/sub?token=private-example"
            try:
                manager.refresh(url)
                status = manager.status()
                self.assertEqual(len(status["nodes"]), 2)
                self.assertNotIn("private-example", json.dumps(status))
                self.assertFalse(status["enabled"])
                manager.enable()
                endpoint = manager.proxy_url()
                config = yaml.safe_load(manager.config_path.read_text(encoding="utf-8"))
                self.assertFalse(config["tun"]["enable"])
                self.assertFalse(config["allow-lan"])
                self.assertEqual(config["mixed-port"], mixed)
                self.assertEqual(config["rules"], ["MATCH,REGISTER"])
                # Controller rejects callers without the random secret.
                with requests.Session() as session:
                    session.trust_env = False
                    self.assertEqual(session.get(f"http://127.0.0.1:{control}/version", timeout=3).status_code, 401)

                def request_node():
                    with requests.Session() as session:
                        session.trust_env = False
                        response = session.get("http://192.0.2.1/fixture", proxies={"http": endpoint}, headers={"Connection": "close"}, timeout=5)
                        response.raise_for_status()
                        return response.json()["node"]

                def idle():
                    until = time.monotonic() + 3
                    while time.monotonic() < until:
                        with manager._locked() as state:
                            if not manager._control(state, "GET", "/connections").get("connections"):
                                return
                        time.sleep(0.05)
                    self.fail("fixture connections did not close")

                self.assertEqual(request_node(), "node-a")
                idle()
                manager.select(status["nodes"][1]["id"])
                self.assertEqual(request_node(), "node-b")
                idle()
                manager.shutdown()
                self.assertTrue(manager.enabled())
                manager.proxy_url()
                self.assertEqual(manager.status()["current_node"], "Local B")
                self.assertEqual(request_node(), "node-b")
                idle()
                # A second manager adopts the project process, rather than another core.
                adopted = MihomoManager(Path(directory), busy_check=lambda: False)
                self.assertEqual(adopted._owned_process().pid, manager._owned_process().pid)
                # Invalid updates leave the existing process and persisted cache intact.
                before = manager.state_path.read_bytes()
                payload["proxies"][0]["type"] = "vless"
                with self.assertRaises(ProxyError):
                    manager.refresh()
                self.assertEqual(before, manager.state_path.read_bytes())
                self.assertEqual(request_node(), "node-b")
                idle()
                payload["proxies"][0]["type"] = "http"
                manager.refresh()
                self.assertEqual(manager.status()["current_node"], "Local B")
                manager.disable()
                self.assertEqual(manager.proxy_url(), "")
                self.assertFalse(manager.status()["running"])
                # Simulated PID reuse must never terminate an unrelated process.
                current = psutil.Process()
                manager.pid_path.write_text(json.dumps({"pid": current.pid, "created": current.create_time()}))
                self.assertIsNone(manager._owned_process())
                manager.shutdown()
                self.assertTrue(current.is_running())
            finally:
                manager.shutdown()
        for pid, created in external:
            self.assertTrue(psutil.pid_exists(pid))
            self.assertEqual(psutil.Process(pid).create_time(), created)


if __name__ == "__main__":
    unittest.main()
