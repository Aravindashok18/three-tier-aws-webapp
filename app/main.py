"""Application tier: a small notes service behind Nginx."""
from __future__ import annotations

import os
import socket
from typing import Optional

from flask import Flask, jsonify, render_template, request
from sqlalchemy.engine import Engine

from app import config, db


def create_app(engine: Optional[Engine] = None) -> Flask:
    app = Flask(__name__)
    app.config["STATIC_BASE_URL"] = os.environ.get("STATIC_BASE_URL", "/static").rstrip("/")
    hostname = socket.gethostname()

    if engine is None:
        engine = db.make_engine(config.database_url(), os.environ.get("DB_SSL_CA"))
        db.init_schema(engine, attempts=int(os.environ.get("DB_INIT_ATTEMPTS", "10")))
    app.extensions["engine"] = engine

    @app.after_request
    def add_headers(response):
        response.headers["X-Served-By"] = hostname  # shows the load balancer rotating between instances
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    @app.get("/health")
    def health():
        """Liveness: the process is up. The load balancer and the cron check use this."""
        return jsonify(status="ok", host=hostname)

    @app.get("/health/ready")
    def ready():
        """Readiness: can this instance reach the database? For dashboards and alarms."""
        if db.ping(engine):
            return jsonify(status="ok", database="up", host=hostname)
        return jsonify(status="degraded", database="down", host=hostname), 503

    @app.get("/")
    def index():
        return render_template(
            "index.html", notes=db.list_notes(engine), host=hostname, static_base=app.config["STATIC_BASE_URL"]
        )

    @app.get("/api/notes")
    def list_notes():
        return jsonify(notes=db.list_notes(engine), host=hostname)

    @app.post("/api/notes")
    def create_note():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return jsonify(error="Send a JSON object with 'author' and 'body'"), 400
        author = str(payload.get("author", "")).strip()
        body = str(payload.get("body", "")).strip()
        if not author or not body:
            return jsonify(error="'author' and 'body' are required"), 400
        if len(author) > db.MAX_AUTHOR or len(body) > db.MAX_BODY:
            return jsonify(error=f"'author' is limited to {db.MAX_AUTHOR} characters and 'body' to {db.MAX_BODY}"), 400
        return jsonify(db.add_note(engine, author, body)), 201

    @app.delete("/api/notes/<int:note_id>")
    def remove_note(note_id: int):
        if not db.delete_note(engine, note_id):
            return jsonify(error="No such note"), 404
        return "", 204

    return app
