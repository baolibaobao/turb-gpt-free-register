"""Parse subscription nodes without importing a provider's routing or listeners."""
from __future__ import annotations

import base64
import copy
import hashlib
import json
from urllib.parse import parse_qs, unquote, urlsplit

import requests
import yaml

MAX_BYTES = 4 * 1024 * 1024
MAX_NODES = 2000
NODE_TYPES = {"ss", "ssr", "vmess", "vless", "trojan", "hysteria", "hysteria2", "tuic", "anytls", "http", "socks5", "wireguard", "snell", "mieru"}


class SubscriptionError(ValueError):
    pass


class _Loader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        # Alias expansion may turn a tiny response into a very large object.
        if self.check_event(yaml.AliasEvent):
            raise SubscriptionError("订阅含 YAML 引用，请使用展开后的 Clash / Mihomo 订阅")
        return super().compose_node(parent, index)


def _b64(value: str) -> str:
    value = "".join(value.split())
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True).decode("utf-8")


def _transport(node: dict, q: dict) -> None:
    security = q.get("security", "")
    if security in {"tls", "reality"}:
        node["tls"] = True
    if q.get("sni") or q.get("peer"):
        node["servername" if node["type"] in {"vmess", "vless"} else "sni"] = q.get("sni") or q["peer"]
    if q.get("fp"):
        node["client-fingerprint"] = q["fp"]
    if q.get("alpn"):
        node["alpn"] = q["alpn"].split(",")
    if q.get("allowInsecure", q.get("insecure", "")).lower() in {"1", "true"}:
        node["skip-cert-verify"] = True
    if security == "reality":
        node["reality-opts"] = {"public-key": q.get("pbk", ""), "short-id": q.get("sid", "")}
    network = q.get("type", q.get("net", "tcp"))
    if network in {"ws", "grpc", "http", "h2"}:
        node["network"] = network
        if network == "ws":
            node["ws-opts"] = {"path": q.get("path", "/"), "headers": {"Host": q.get("host", node["server"])}}
        elif network == "grpc":
            node["grpc-opts"] = {"grpc-service-name": q.get("serviceName", q.get("path", ""))}
        else:
            node[f"{network}-opts"] = {"path": [q.get("path", "/")], "headers": {"Host": [q.get("host", node["server"])]}} if network == "http" else {"path": q.get("path", "/"), "host": [q.get("host", node["server"])]}
    elif network not in {"tcp", "", "none"}:
        raise SubscriptionError("该传输协议请使用 Clash / Mihomo 格式订阅")


def _uri(value: str) -> dict:
    if value.startswith("vmess://"):
        data = json.loads(_b64(value[8:]))
        node = {"type": "vmess", "name": data.get("ps"), "server": data["add"], "port": int(data["port"]), "uuid": data["id"], "alterId": int(data.get("aid", 0)), "cipher": data.get("scy", "auto"), "udp": True}
        _transport(node, {**data, "security": data.get("tls", ""), "type": data.get("net", "tcp")})
        return node
    if value.startswith("ss://") and "@" not in value.split("#")[0]:
        body, _, fragment = value[5:].partition("#")
        value = "ss://" + _b64(body) + ("#" + fragment if fragment else "")
    u = urlsplit(value)
    q = {k: v[-1] for k, v in parse_qs(u.query).items()}
    kind = {"hy2": "hysteria2", "socks": "socks5", "socks5h": "socks5", "https": "http"}.get(u.scheme, u.scheme)
    if kind not in {"ss", "vmess", "vless", "trojan", "hysteria2", "tuic", "anytls", "http", "socks5"}:
        raise SubscriptionError("该 URI 协议请使用 Clash / Mihomo 格式订阅")
    node = {"type": kind, "name": unquote(u.fragment), "server": u.hostname, "port": u.port or (443 if u.scheme == "https" else 0), "udp": True}
    username, password = unquote(u.username or ""), unquote(u.password or "")
    if kind == "ss":
        if not password:
            username, password = _b64(username).split(":", 1)
        node.update(cipher=username, password=password)
        if q.get("plugin"):
            parts = q["plugin"].split(";")
            plugin = parts[0]
            if plugin not in {"obfs-local", "simple-obfs", "v2ray-plugin"}:
                raise SubscriptionError("该 SS 插件请使用 Clash / Mihomo 格式订阅")
            opts = dict(part.split("=", 1) if "=" in part else (part, True) for part in parts[1:])
            if plugin in {"obfs-local", "simple-obfs"}:
                node.update(plugin="obfs", **{"plugin-opts": {"mode": opts.get("obfs", "http"), "host": opts.get("obfs-host", "")}})
            else:
                node.update(plugin=plugin, **{"plugin-opts": opts})
    elif kind in {"vmess", "vless"}:
        node["uuid"] = username
        if kind == "vmess":
            node.update(cipher="auto", alterId=0)
        if q.get("flow"):
            node["flow"] = q["flow"]
        _transport(node, q)
    elif kind == "tuic":
        node.update(uuid=username, password=password, **{"congestion-controller": q.get("congestion_control", "cubic")})
        _transport(node, q)
    elif kind in {"trojan", "hysteria2", "anytls"}:
        node["password"] = username + (":" + password if password else "")
        _transport(node, q)
        if kind == "hysteria2" and q.get("obfs"):
            node.update(obfs=q["obfs"], **{"obfs-password": q.get("obfs-password", "")})
    else:
        if username:
            node.update(username=username, password=password)
        if u.scheme == "https":
            node["tls"] = True
        node.pop("udp", None)
    return node


def parse_subscription(text: str) -> tuple[list[dict], list[str]]:
    """Return stable IDs and full node credentials; callers expose only summaries."""
    if len(text.encode("utf-8")) > MAX_BYTES:
        raise SubscriptionError("订阅超过 4 MiB")
    text = text.strip().lstrip("\ufeff")
    try:
        if "://" not in text and not text.startswith(("{", "[", "proxies:")):
            try:
                text = _b64(text)
            except (ValueError, UnicodeError):
                pass
        if text.startswith(("{", "[")) or "proxies:" in text:
            data = yaml.load(text, Loader=_Loader)
            raw = data.get("proxies", []) if isinstance(data, dict) else data
            if not isinstance(raw, list):
                raise SubscriptionError("订阅中的 proxies 应为节点列表")
        else:
            raw = [line.strip() for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]
        if len(raw) > MAX_NODES:
            raise SubscriptionError("订阅节点超过 2000 个")
        nodes, warnings, names = [], [], set()
        for index, item in enumerate(raw, 1):
            try:
                node = _uri(item) if isinstance(item, str) else copy.deepcopy(item)
                if not isinstance(node, dict) or node.get("type") not in NODE_TYPES:
                    raise ValueError()
                if not isinstance(node.get("server"), str) or not node["server"].strip():
                    raise ValueError()
                node["port"] = int(node["port"])
                if not 1 <= node["port"] <= 65535:
                    raise ValueError()
                # Reject cross-node routes and references to arbitrary local files.
                if any(k in node for k in ("dialer-proxy", "interface-name", "routing-mark", "certificate", "private-key", "ca")):
                    raise SubscriptionError("含跨节点路由或本地证书路径")
                name = str(node.get("name") or f"{node['type']}-{index}").strip()[:180]
                base = name
                suffix = 1
                while name in names or name in {"DIRECT", "REJECT", "REGISTER"}:
                    suffix += 1
                    name = f"{base} ({suffix})"
                names.add(name)
                node["name"] = name
                # Validate JSON-compatible values and cap pathological nesting/expansion.
                encoded = json.dumps(node, ensure_ascii=False, allow_nan=False)
                if len(encoded) > 65536:
                    raise ValueError()
                identity = json.dumps([name, node["type"], node["server"], node["port"]], ensure_ascii=False)
                nodes.append({"id": hashlib.sha256(identity.encode()).hexdigest()[:24], "config": node})
            except (ValueError, KeyError, TypeError, UnicodeError, RecursionError):
                warnings.append(f"第 {index} 个节点格式暂未支持或字段有误；推荐使用 Clash / Mihomo 订阅")
        if not nodes:
            raise SubscriptionError("订阅未解析出可用节点；请选择 Clash / Mihomo 格式的订阅链接")
        return nodes, warnings
    except SubscriptionError:
        raise
    except (yaml.YAMLError, ValueError, TypeError, RecursionError) as exc:
        raise SubscriptionError("订阅格式解析失败；请检查订阅格式") from exc


def download_subscription(url: str, proxy: str = "") -> str:
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
            raise SubscriptionError("请输入 HTTP / HTTPS 订阅链接")
        with requests.Session() as session:
            session.trust_env = False
            if proxy:
                session.proxies = {"http": proxy, "https": proxy}
            with session.get(url, headers={"User-Agent": "clash.meta"}, timeout=(10, 25), stream=True) as response:
                response.raise_for_status()
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise SubscriptionError("订阅超过 4 MiB")
                    chunks.append(chunk)
                return b"".join(chunks).decode("utf-8-sig")
    except SubscriptionError:
        raise
    except (requests.RequestException, UnicodeError, ValueError) as exc:
        # Requests exception text includes private URL query strings.
        raise SubscriptionError("订阅下载失败：检查链接、有效期和下载方式") from exc
