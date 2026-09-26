"""Authenticated UI/API for the project's independent Mihomo instance."""
from flask import Blueprint, jsonify, render_template, request

from core.mihomo_proxy import ProxyError, get_manager
from core.proxy_subscription import SubscriptionError


def create_proxy_blueprint(manager=None):
    manager = manager or get_manager()
    bp = Blueprint("proxy_subscription", __name__)

    @bp.get("/proxy-subscription")
    def page():
        return render_template("proxy_subscription.html")

    @bp.get("/api/proxy-subscription")
    def status():
        return jsonify(ok=True, **manager.status())

    @bp.post("/api/proxy-subscription/<action>")
    def change(action):
        # JSON-only mutations reject cross-site form submissions.
        if not request.is_json:
            return jsonify(ok=False, error="请使用 JSON 请求"), 415
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify(ok=False, error="请求内容应为 JSON 对象"), 400
        if action == "refresh":
            manager.refresh(data.get("url", ""), data.get("download_mode", "direct"))
        elif action == "select":
            manager.select(data.get("node_id", ""))
        elif action == "enable":
            manager.enable()
        elif action == "disable":
            manager.disable()
        elif action == "ports":
            manager.configure_ports(data.get("mixed_port"), data.get("controller_port"))
        elif action == "test":
            return jsonify(ok=True, **manager.test_connection())
        else:
            return jsonify(ok=False, error="操作不存在"), 404
        return jsonify(ok=True, **manager.status())

    @bp.errorhandler(ProxyError)
    @bp.errorhandler(SubscriptionError)
    def expected_error(error):
        return jsonify(ok=False, error=str(error)), 409 if isinstance(error, ProxyError) else 400

    @bp.errorhandler(OSError)
    def filesystem_error(error):
        return jsonify(ok=False, error="代理文件或进程操作失败，请检查核心文件、端口和目录权限"), 500

    @bp.after_request
    def private_response(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return bp
