import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Flask

from core.mihomo_proxy import MihomoManager, ProxyError, _atomic_json
from core.proxy_subscription import SubscriptionError, parse_subscription
from webui.auth import init_auth, register_auth_routes
from webui.proxy_subscription import create_proxy_blueprint


class SubscriptionTests(unittest.TestCase):
    def test_clash_retains_protocol_fields_and_stable_ids(self):
        raw = '''mixed-port: 7897
tun: {enable: true}
proxies:
  - name: Example
    type: vless
    server: example.invalid
    port: 443
    uuid: fake-uuid
    tls: true
    reality-opts: {public-key: example-key, short-id: abc}
    network: ws
    ws-opts: {path: /ws, headers: {Host: example.invalid}}
  - name: Example
    type: http
    server: localhost
    port: 8000
'''
        nodes, warnings = parse_subscription(raw)
        self.assertEqual(warnings, [])
        self.assertEqual(nodes[0]["config"]["reality-opts"]["public-key"], "example-key")
        self.assertEqual(nodes[0]["config"]["ws-opts"]["path"], "/ws")
        self.assertNotEqual(nodes[0]["config"]["name"], nodes[1]["config"]["name"])
        self.assertEqual(nodes, parse_subscription(raw)[0])
        self.assertNotIn("tun", nodes[0]["config"])

    def test_base64_uri_list_preserves_credentials_and_transport(self):
        ss = base64.urlsafe_b64encode(b"aes-128-gcm:example-password").decode().rstrip("=")
        raw = f"ss://{ss}@example.invalid:443#SS\nvless://example-uuid@example.invalid:443?security=reality&pbk=pub&sid=ab&type=grpc&serviceName=svc#VL\nhttps://user:p%40ss@host.invalid:443#HTTP"
        nodes, warnings = parse_subscription(base64.b64encode(raw.encode()).decode())
        self.assertFalse(warnings)
        self.assertEqual(nodes[0]["config"]["password"], "example-password")
        self.assertEqual(nodes[1]["config"]["grpc-opts"]["grpc-service-name"], "svc")
        self.assertEqual(nodes[1]["config"]["reality-opts"]["public-key"], "pub")
        self.assertEqual(nodes[2]["config"]["password"], "p@ss")
        self.assertTrue(nodes[2]["config"]["tls"])

    def test_vmess_json(self):
        obj = {"ps": "vm", "add": "example.invalid", "port": "443", "id": "example-uuid", "aid": "0", "net": "ws", "tls": "tls", "path": "/ws", "host": "host.invalid"}
        nodes, _ = parse_subscription("vmess://" + base64.b64encode(json.dumps(obj).encode()).decode())
        self.assertEqual(nodes[0]["config"]["ws-opts"]["headers"]["Host"], "host.invalid")
        self.assertTrue(nodes[0]["config"]["tls"])

    def test_bad_items_report_index_without_credentials(self):
        nodes, warnings = parse_subscription("http://host.invalid:9000#ok\nvless://secret@host.invalid:99999#bad")
        self.assertEqual(len(nodes), 1)
        self.assertNotIn("secret", str(warnings))
        self.assertIn("2", warnings[0])

    def test_unsafe_or_empty_input(self):
        for value in ("", "<html>expired</html>", "proxies: &loop [*loop]", "proxies: [{name: bad, type: http, server: localhost, port: 80, dialer-proxy: DIRECT}]", "proxies: [{name: bad, type: http, server: localhost, port: 80, ca: C:/private.pem}]"):
            with self.subTest(value=value), self.assertRaises(SubscriptionError):
                parse_subscription(value)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.manager = MihomoManager(Path(self.temp.name), busy_check=lambda: False)

    def test_disabled_keeps_original_pool_and_enabled_errors_propagate(self):
        from config import proxy
        with patch("core.mihomo_proxy._manager", self.manager), patch.object(proxy, "PROXY_POOL", ["http://legacy:7897"]):
            self.assertEqual(proxy.pick_proxy(), "http://legacy:7897")
            with patch.object(self.manager, "proxy_url", side_effect=ProxyError("core failed")):
                with self.assertRaises(ProxyError):
                    proxy.pick_proxy()

    def test_managed_endpoint_skips_legacy_upstream_and_plan_direct_mode(self):
        from config import proxy
        from core.proxy_chain import open_proxy_pool_proxy
        from core.chatgpt_plan import resolve_plan_check_route
        endpoint = self.manager.status()["endpoint"]
        with self.manager._locked() as state:
            state["enabled"] = True
            _atomic_json(self.manager.state_path, state)
        with patch("core.mihomo_proxy._manager", self.manager), patch.object(self.manager, "proxy_url", return_value=endpoint), patch.object(proxy, "PROXY_POOL_UPSTREAM_PROXY", "http://legacy:7897"), patch.object(proxy, "PLAN_CHECK_PROXY_MODE", "direct"):
            self.assertEqual(open_proxy_pool_proxy(), (endpoint, None))
            route = resolve_plan_check_route()
            self.assertEqual(route["proxy"], endpoint)
            self.assertEqual(route["upstream_proxy"], "")
            self.assertEqual(resolve_plan_check_route("")["network_route"], "direct")
            with patch.object(self.manager, "proxy_url", side_effect=ProxyError("core failed")):
                with self.assertRaises(ProxyError):
                    resolve_plan_check_route()

    def test_ports_and_status_have_no_private_data(self):
        self.manager.configure_ports(21898, 23098)
        status = self.manager.status()
        self.assertEqual(status["endpoint"], "http://127.0.0.1:21898")
        self.assertNotIn("secret", status)
        self.assertNotIn("url", status)
        for ports in ((80, 9090), (9090, 9090), (None, "bad")):
            with self.assertRaises(ProxyError):
                self.manager.configure_ports(*ports)

    def test_corrupt_state_fails_closed(self):
        self.manager.state_path.write_text("broken", encoding="utf-8")
        with self.assertRaises(ProxyError):
            self.manager.proxy_url()

    def test_failed_refresh_preserves_cache_and_url(self):
        self.manager.configure_ports(21898, 23098)
        previous = self.manager.state_path.read_bytes()
        with patch("core.mihomo_proxy.download_subscription", side_effect=SubscriptionError("download failed")):
            with self.assertRaises(SubscriptionError):
                self.manager.refresh("https://example.invalid/?token=example")
        self.assertEqual(previous, self.manager.state_path.read_bytes())

    def test_api_auth_json_and_redaction(self):
        app = Flask(__name__)
        init_auth(app, auth_code="example-test-code")
        register_auth_routes(app)
        app.register_blueprint(create_proxy_blueprint(self.manager))
        client = app.test_client()
        path = "/api/proxy-subscription"
        self.assertEqual(client.get(path).status_code, 401)
        headers = {"X-Auth-Code": "example-test-code"}
        self.assertEqual(client.get(path, headers=headers).status_code, 200)
        self.assertEqual(client.post(path + "/enable", headers=headers, data={}).status_code, 415)
        self.assertEqual(client.post(path + "/enable", headers=headers, json={}).status_code, 409)
        self.assertEqual(client.post(path + "/select", headers=headers, json=[]).status_code, 400)

    def test_busy_task_prevents_mutation(self):
        with patch.object(self.manager, "busy_check", return_value=True):
            with self.assertRaises(ProxyError):
                self.manager.refresh("https://example.invalid")
            with self.assertRaises(ProxyError):
                self.manager.disable()

    def test_unresponsive_owned_core_can_be_disabled(self):
        with patch.object(self.manager, "_owned_process", return_value=object()), patch.object(self.manager, "_control", side_effect=ProxyError("unresponsive")), patch.object(self.manager, "_stop") as stop:
            self.manager.disable()
            stop.assert_called_once()
        self.assertFalse(self.manager.enabled())

    def test_new_roxy_profile_uses_managed_node_with_legacy_toggle_off(self):
        from core import roxybrowser_client as roxy
        with patch("core.mihomo_proxy.managed_enabled", return_value=True), patch("core.mihomo_proxy.managed_proxy_url", return_value="http://127.0.0.1:17898"), patch("core.mihomo_proxy.is_managed_proxy", return_value=True), patch.object(roxy._cfg, "ROXY_CREATE_USE_PROXY_POOL", False), patch.object(roxy._cfg, "ROXY_API_BASE", "http://127.0.0.1:50100"), patch.object(roxy._cfg, "ROXY_WORKSPACE_ID", "fixture-workspace"), patch.object(roxy, "_wait_for_create_slot"):
            client = roxy.RoxyBrowserClient()
            with patch.object(client, "request", return_value={"data": {"dirId": "123"}}) as request:
                self.assertEqual(client.create_profile({"proxyInfo": {"host": "legacy", "port": "7897"}}), "123")
                info = request.call_args.kwargs["json_body"]["proxyInfo"]
                self.assertEqual(info["host"], "127.0.0.1")
                self.assertEqual(str(info["port"]), "17898")

    def test_roxy_existing_or_remote_profiles_get_clear_error(self):
        from core import roxybrowser_client as roxy
        with patch("core.mihomo_proxy.managed_enabled", return_value=True):
            client = roxy.RoxyBrowserClient()
            with patch.object(client, "request") as request:
                with self.assertRaisesRegex(RuntimeError, "ROXY_PROFILE_ID"):
                    client.open_profile("fixture-profile")
                with patch.object(roxy._cfg, "ROXY_API_BASE", "http://remote.invalid:50100"):
                    with self.assertRaisesRegex(RuntimeError, "本机 Roxy"):
                        client.create_profile()
                request.assert_not_called()


if __name__ == "__main__":
    unittest.main()
