"""Project-owned Mihomo process, subscription cache and selected-node routing."""
from __future__ import annotations

import atexit
import copy
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from urllib.parse import quote

import psutil
import requests
import yaml

from core.proxy_subscription import SubscriptionError, download_subscription, parse_subscription

ROOT = Path(__file__).resolve().parents[1]


class ProxyError(RuntimeError):
    pass


def _atomic_json(path: Path, data: dict) -> None:
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)


def _clear_geo_cache() -> None:
    module = sys.modules.get("core.session")
    if module is not None and hasattr(module, "_GEO_CACHE_LOCK"):
        with module._GEO_CACHE_LOCK:
            module._GEO_CACHE.clear()


def _registration_busy() -> bool:
    from core import db
    return db.has_active_proxy_work()


class MihomoManager:
    def __init__(self, directory: Path | None = None, busy_check=None):
        self.directory = Path(directory or ROOT / "run" / "mihomo").resolve()
        self.state_path = self.directory / "state.json"
        self.config_path = self.directory / "config.yaml"
        self.pid_path = self.directory / "process.json"
        self.core_path = self.directory / "bin" / ("mihomo.exe" if os.name == "nt" else "mihomo")
        self.lock = threading.RLock()
        self.busy_check = busy_check or _registration_busy
        self.last_error = ""
        self._children: dict[int, subprocess.Popen] = {}

    @contextmanager
    def _locked(self):
        # Serialize WebUI and CLI access as well as concurrent HTTP requests.
        with self.lock:
            self.directory.mkdir(parents=True, exist_ok=True)
            with (self.directory / "manager.lock").open("a+b") as handle:
                handle.seek(0, os.SEEK_END)
                if handle.tell() == 0:
                    handle.write(b"0")
                    handle.flush()
                deadline = time.monotonic() + 50
                while True:
                    try:
                        handle.seek(0)
                        if os.name == "nt":
                            import msvcrt
                            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                        else:
                            import fcntl
                            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except OSError:
                        if time.monotonic() >= deadline:
                            raise ProxyError("代理配置正在更新，请稍后再试") from None
                        time.sleep(0.1)
                try:
                    yield self._read()
                finally:
                    handle.seek(0)
                    if os.name == "nt":
                        import msvcrt
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    def _read(self) -> dict:
        if not self.state_path.exists():
            return {"enabled": False, "url": "", "nodes": [], "selected": "", "mixed_port": 17898, "controller_port": 19098, "secret": secrets.token_urlsafe(32), "updated_at": None, "warnings": [], "download_mode": "direct"}
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            if not isinstance(state.get("nodes"), list) or not isinstance(state.get("enabled"), bool):
                raise ValueError()
            return state
        except (ValueError, OSError, AttributeError):
            raise ProxyError("代理配置文件读取失败，请检查 run/mihomo/state.json 或恢复备份") from None

    def _owned_process(self):
        if not self.pid_path.exists():
            return None
        try:
            info = json.loads(self.pid_path.read_text(encoding="utf-8"))
            process = psutil.Process(info["pid"])
            args = process.cmdline()
            config_arg = args[args.index("-f") + 1]
            if (abs(process.create_time() - info["created"]) < 0.1
                    and Path(process.exe()).resolve() == self.core_path.resolve()
                    and Path(config_arg).resolve() == self.config_path):
                return process
        except (psutil.Error, OSError, ValueError, KeyError, IndexError, TypeError):
            pass
        return None

    def _control(self, state, method, path, **kwargs):
        try:
            with requests.Session() as session:
                session.trust_env = False
                response = session.request(method, f"http://127.0.0.1:{state['controller_port']}{path}", headers={"Authorization": f"Bearer {state['secret']}"}, timeout=3, **kwargs)
                response.raise_for_status()
                return response.json() if response.content else {}
        except (requests.RequestException, ValueError):
            raise ProxyError("项目 Mihomo 控制接口连接失败，请检查端口占用或重新启用") from None

    def _assert_idle(self, state, *, allow_unhealthy=False):
        if self.busy_check():
            raise ProxyError("仍有任务待处理或运行中，请结束任务后再切换、刷新或停用代理")
        if self._owned_process():
            try:
                connections = self._control(state, "GET", "/connections").get("connections") or []
            except ProxyError:
                if allow_unhealthy:
                    return
                raise
            if connections:
                raise ProxyError("项目代理仍有活动连接，请关闭相关浏览器或等待请求结束后重试")

    @staticmethod
    def _endpoint(state):
        return f"http://127.0.0.1:{state['mixed_port']}"

    def _configuration(self, state):
        selected = next((n for n in state["nodes"] if n["id"] == state["selected"]), None)
        if not selected:
            raise ProxyError("请先导入订阅并选择一个节点")
        ordered = [selected] + [n for n in state["nodes"] if n["id"] != selected["id"]]
        return {
            "mixed-port": state["mixed_port"], "bind-address": "127.0.0.1", "allow-lan": False,
            "external-controller": f"127.0.0.1:{state['controller_port']}", "secret": state["secret"],
            "mode": "rule", "log-level": "warning", "ipv6": False,
            "tun": {"enable": False}, "dns": {"enable": False},
            "profile": {"store-selected": False, "store-fake-ip": False},
            "proxies": [n["config"] for n in ordered],
            "proxy-groups": [{"name": "REGISTER", "type": "select", "proxies": [n["config"]["name"] for n in ordered]}],
            "rules": ["MATCH,REGISTER"],
        }

    def _validate(self, state):
        if not self.core_path.is_file():
            raise ProxyError("缺少 Mihomo 核心，请将核心放入 run/mihomo/bin/mihomo.exe")
        path = self.directory / "candidate.yaml"
        path.write_text(yaml.safe_dump(self._configuration(state), allow_unicode=True, sort_keys=False), encoding="utf-8")
        try:
            result = subprocess.run([str(self.core_path), "-t", "-d", str(self.directory), "-f", str(path)], capture_output=True, timeout=15, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            if result.returncode:
                raise ProxyError("Mihomo 校验订阅节点失败；请使用兼容当前核心的 Clash / Mihomo 订阅")
            return path.read_text(encoding="utf-8")
        except (OSError, subprocess.TimeoutExpired):
            raise ProxyError("Mihomo 核心运行失败或校验超时") from None
        finally:
            path.unlink(missing_ok=True)

    def _stop(self):
        process = self._owned_process()
        if process:
            try:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except psutil.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                child = self._children.pop(process.pid, None)
                if child:
                    child.wait(timeout=1)
            except psutil.NoSuchProcess:
                pass
        self.pid_path.unlink(missing_ok=True)

    def _start(self, state, validated=None):
        if self._owned_process():
            self._control(state, "GET", "/version")
            return
        content = validated if validated is not None else self._validate(state)
        for port in (state["mixed_port"], state["controller_port"]):
            try:
                with socket.socket() as sock:
                    sock.bind(("127.0.0.1", port))
            except OSError:
                raise ProxyError(f"项目代理端口 {port} 已被占用，请在高级设置中更换") from None
        self.config_path.write_text(content, encoding="utf-8")
        log_path = self.directory / "core.log"
        if log_path.exists() and log_path.stat().st_size > 2 * 1024 * 1024:
            log_path.replace(self.directory / "core.previous.log")
        with log_path.open("ab") as output:
            child = subprocess.Popen([str(self.core_path), "-d", str(self.directory), "-f", str(self.config_path)], stdin=subprocess.DEVNULL, stdout=output, stderr=output, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self._children[child.pid] = child
        try:
            _atomic_json(self.pid_path, {"pid": child.pid, "created": psutil.Process(child.pid).create_time()})
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and child.poll() is None:
                try:
                    self._control(state, "GET", "/version")
                    self.last_error = ""
                    return
                except ProxyError:
                    time.sleep(0.15)
            raise ProxyError("项目 Mihomo 启动失败，请检查核心或端口；日志位于 run/mihomo/core.log")
        except Exception:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=5)
            self._children.pop(child.pid, None)
            self.pid_path.unlink(missing_ok=True)
            raise

    def _replace(self, old, new):
        validated = self._validate(new)
        running = bool(self._owned_process())
        try:
            self._stop()
            if new["enabled"]:
                self._start(new, validated)
            _atomic_json(self.state_path, new)
        except Exception as exc:
            self._stop()
            if running:
                try:
                    self._start(old)
                except Exception:
                    self.last_error = "更新失败，原节点重启也失败，请重新启用项目代理"
            raise ProxyError("代理更新失败，已保留原订阅配置。" + str(exc) if isinstance(exc, ProxyError) else "代理配置保存失败，已保留原配置") from None
        _clear_geo_cache()

    def status(self):
        with self._locked() as state:
            running = bool(self._owned_process())
            healthy, current = False, ""
            if running:
                try:
                    current = self._control(state, "GET", "/proxies/REGISTER").get("now", "")
                    healthy = True
                except ProxyError:
                    pass
            return {
                "enabled": state["enabled"], "running": running, "healthy": healthy, "current_node": current,
                "has_subscription": bool(state["url"]), "selected": state["selected"],
                "nodes": [{"id": n["id"], "name": n["config"]["name"], "type": n["config"]["type"]} for n in state["nodes"]],
                "mixed_port": state["mixed_port"], "controller_port": state["controller_port"],
                "endpoint": self._endpoint(state), "core_installed": self.core_path.is_file(),
                "updated_at": state["updated_at"], "warnings": state.get("warnings", []),
                "download_mode": state.get("download_mode", "direct"), "error": self.last_error,
            }

    def refresh(self, url="", download_mode="direct"):
        if download_mode not in {"direct", "managed"}:
            raise ProxyError("下载方式请选择 direct 或 managed")
        with self._locked() as old:
            self._assert_idle(old)
            url = str(url or old["url"]).strip()
            if not url:
                raise ProxyError("请填写订阅链接")
            proxy = ""
            if download_mode == "managed":
                if not old["enabled"]:
                    raise ProxyError("经项目节点下载需要先启用一个已缓存节点；首次导入请选择直连")
                self._start(old)
                proxy = self._endpoint(old)
            nodes, warnings = parse_subscription(download_subscription(url, proxy))
            new = {**old, "url": url, "nodes": nodes, "warnings": warnings, "updated_at": time.strftime("%Y-%m-%d %H:%M:%S"), "download_mode": download_mode}
            if old["selected"] not in {n["id"] for n in nodes}:
                if old["enabled"]:
                    raise ProxyError("订阅中已移除当前节点。请先停用项目代理，再刷新并选择新节点；原缓存仍保留")
                new["selected"] = nodes[0]["id"]
            self._replace(old, new)

    def select(self, node_id):
        with self._locked() as state:
            node = next((n for n in state["nodes"] if n["id"] == node_id), None)
            if not node:
                raise ProxyError("节点不存在，请刷新页面后重新选择")
            if state["selected"] == node_id:
                return
            self._assert_idle(state)
            old = copy.deepcopy(state)
            state["selected"] = node_id
            if state["enabled"]:
                self._start(old)
                self._control(state, "PUT", "/proxies/REGISTER", json={"name": node["config"]["name"]})
            try:
                # Regenerate selected-first ordering on next process start.
                _atomic_json(self.state_path, state)
            except OSError:
                if state["enabled"]:
                    previous = next(n for n in old["nodes"] if n["id"] == old["selected"])
                    self._control(old, "PUT", "/proxies/REGISTER", json={"name": previous["config"]["name"]})
                raise ProxyError("节点选择保存失败，已恢复原节点") from None
            _clear_geo_cache()

    def enable(self):
        with self._locked() as old:
            if not old["enabled"]:
                self._assert_idle(old)
            new = {**old, "enabled": True}
            self._start(new)
            try:
                _atomic_json(self.state_path, new)
            except OSError:
                if not old["enabled"]:
                    self._stop()
                raise ProxyError("项目代理启用状态保存失败") from None

    def disable(self):
        with self._locked() as state:
            self._assert_idle(state, allow_unhealthy=True)
            self._stop()
            state["enabled"] = False
            _atomic_json(self.state_path, state)
            _clear_geo_cache()

    def configure_ports(self, mixed_port, controller_port):
        try:
            ports = [int(mixed_port), int(controller_port)]
            if any(not 1024 <= port <= 65535 for port in ports) or ports[0] == ports[1]:
                raise ValueError()
        except (TypeError, ValueError):
            raise ProxyError("请选择两个不同的 1024–65535 端口") from None
        with self._locked() as state:
            if state["enabled"] or self._owned_process():
                raise ProxyError("请先停用项目代理再修改端口")
            state.update(mixed_port=ports[0], controller_port=ports[1])
            _atomic_json(self.state_path, state)

    def enabled(self):
        # Cheap read for browser setup; writes replace the complete file atomically.
        with self.lock:
            return self._read()["enabled"]

    def proxy_url(self):
        with self._locked() as state:
            if not state["enabled"]:
                return ""
            self._start(state)
            return self._endpoint(state)

    def is_endpoint(self, url):
        with self.lock:
            state = self._read()
            return state["enabled"] and url == self._endpoint(state)

    def test_connection(self):
        with self._locked() as state:
            if not state["enabled"]:
                raise ProxyError("请先启用项目代理")
            self._start(state)
            start = time.monotonic()
            try:
                with requests.Session() as session:
                    session.trust_env = False
                    proxy = self._endpoint(state)
                    response = session.get("https://www.cloudflare.com/cdn-cgi/trace", proxies={"http": proxy, "https": proxy}, timeout=(10, 15))
                    response.raise_for_status()
                    data = dict(line.split("=", 1) for line in response.text.splitlines() if "=" in line)
                    if not data.get("ip"):
                        raise ValueError()
                    return {"ip": data["ip"], "country": data.get("loc", ""), "elapsed_ms": round((time.monotonic() - start) * 1000)}
            except (requests.RequestException, ValueError):
                raise ProxyError("出口测试失败，请检查节点是否可用，或换一个节点重试") from None

    def shutdown(self):
        # Preserve enabled + node for the next one-click launch.
        with self._locked():
            self._stop()


_manager = MihomoManager()


def get_manager():
    return _manager


def managed_enabled():
    return get_manager().enabled()


def managed_proxy_url():
    return get_manager().proxy_url()


def is_managed_proxy(url):
    return get_manager().is_endpoint(url)


def restore_managed_proxy():
    manager = get_manager()
    try:
        manager.proxy_url()
    except (ProxyError, OSError) as exc:
        manager.last_error = str(exc) if isinstance(exc, ProxyError) else "项目代理启动失败，请检查文件权限"
    atexit.register(manager.shutdown)
