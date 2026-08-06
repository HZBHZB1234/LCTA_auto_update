"""Flask HTTP 接口。"""
from __future__ import annotations

import threading
from typing import Any

from flask import Flask, jsonify

from .checker import StatusHolder


def create_app(
    holder: StatusHolder, manual_event: threading.Event
) -> Flask:
    app = Flask(__name__)

    @app.get("/api/status")
    def status() -> Any:
        payload = holder.get()
        if payload is None:
            return jsonify({"error": "no token found yet"}), 503
        return jsonify(payload)

    @app.post("/api/check")
    def check() -> Any:
        manual_event.set()
        return jsonify({"status": "accepted"}), 202

    @app.errorhandler(404)
    def not_found(_error: Any) -> Any:
        return jsonify({"error": "not found"}), 404

    return app
