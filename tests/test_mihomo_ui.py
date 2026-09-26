"""Browser smoke test against an isolated app and real project core."""
import json
import logging
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask
from werkzeug.serving import make_server

from core.mihomo_proxy import ROOT, MihomoManager
from webui.auth import init_auth, register_auth_routes
from webui.proxy_subscription import create_proxy_blueprint
from test_mihomo_integration import CORE, free_ports

EDGE = Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe")


@unittest.skipUnless(CORE.is_file() and EDGE.is_file(), "Local Mihomo/Edge required")
class ProxyBrowserTests(unittest.TestCase):
    def test_import_select_enable_disable_and_escape_node_name(self):
        from playwright.sync_api import sync_playwright
        with tempfile.TemporaryDirectory(prefix="proxy-ui-") as directory:
            manager = MihomoManager(Path(directory), busy_check=lambda: False)
            manager.core_path.parent.mkdir(parents=True)
            shutil.copy2(CORE, manager.core_path)
            manager.configure_ports(*free_ports())
            app = Flask(__name__, template_folder=str(ROOT / "webui" / "templates"), static_folder=str(ROOT / "webui" / "static"))
            init_auth(app, auth_code="fixture-login-code")
            register_auth_routes(app)
            app.register_blueprint(create_proxy_blueprint(manager))
            server = make_server("127.0.0.1", 0, app, threaded=True)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            logging.getLogger("werkzeug").setLevel(logging.ERROR)
            payload = json.dumps({"proxies": [
                {"name": "<img src=x onerror=window.PWNED=1>", "type": "http", "server": "127.0.0.1", "port": 9991},
                {"name": "Second node", "type": "http", "server": "127.0.0.1", "port": 9992},
            ]})
            try:
                with patch("core.mihomo_proxy.download_subscription", return_value=payload), sync_playwright() as playwright:
                    browser = playwright.chromium.launch(executable_path=str(EDGE), headless=True)
                    try:
                        page = browser.new_page(viewport={"width": 1100, "height": 900})
                        errors = []
                        page.on("pageerror", lambda error: errors.append(str(error)))
                        base = f"http://127.0.0.1:{server.server_port}"
                        page.goto(base + "/proxy-subscription")
                        self.assertIn("/login", page.url)
                        page.locator('[name="auth_code"]').fill("fixture-login-code")
                        page.locator('button[type="submit"]').click()
                        page.wait_for_url(base + "/proxy-subscription")
                        page.wait_for_function("document.getElementById('status').textContent === '未启用'")
                        page.locator("#url").fill("https://example.invalid/sub?token=private-example")
                        page.locator("#refresh").click()
                        page.wait_for_function("document.getElementById('count').textContent.includes('2 / 2')")
                        self.assertEqual(page.locator("#url").input_value(), "")
                        self.assertIsNone(page.evaluate("window.PWNED"))
                        page.locator("#nodes").select_option(index=1)
                        page.locator("#select").click()
                        page.wait_for_function("document.getElementById('message').textContent === '节点选择已保存。'")
                        self.assertEqual(manager.status()["nodes"][1]["id"], manager.status()["selected"])
                        page.locator("#enable").click()
                        page.wait_for_function("document.getElementById('status').textContent === '已启用'")
                        self.assertEqual(page.locator("#current").inner_text(), "Second node")
                        page.reload()
                        page.wait_for_function("document.getElementById('status').textContent === '已启用'")
                        self.assertEqual(page.locator("#nodes").input_value(), manager.status()["selected"])
                        page.set_viewport_size({"width": 390, "height": 844})
                        self.assertTrue(page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
                        page.locator("#disable").click()
                        page.wait_for_function("document.getElementById('status').textContent === '未启用'")
                        self.assertFalse(manager.status()["running"])
                        self.assertEqual(errors, [])
                    finally:
                        browser.close()
            finally:
                manager.shutdown()
                server.shutdown()
                server.server_close()


if __name__ == "__main__":
    unittest.main()
