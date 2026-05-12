import os
import time
import threading
import logging
import datetime
import requests
from flask import Flask, jsonify

logger = logging.getLogger(__name__)

_flask_app = Flask(__name__)
_movie_count_fn = lambda: 0
_start_time = datetime.datetime.utcnow()

SELF_PING_INTERVAL = 60   # seconds between self-pings


def _uptime() -> str:
    delta = datetime.datetime.utcnow() - _start_time
    h, rem = divmod(int(delta.total_seconds()), 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m}m {s}s"


@_flask_app.route("/")
def health():
    return (
        f"<h2>Bot is alive</h2>"
        f"<p>Movies indexed: <strong>{_movie_count_fn()}</strong></p>"
        f"<p>Uptime: <strong>{_uptime()}</strong></p>"
        f"<p>Started: {_start_time.strftime('%Y-%m-%d %H:%M:%S')} UTC</p>"
    ), 200


@_flask_app.route("/ping")
def ping():
    return "OK", 200


@_flask_app.route("/status")
def status():
    return jsonify({
        "status": "running",
        "movies_indexed": _movie_count_fn(),
        "uptime": _uptime(),
        "started_at": _start_time.isoformat() + "Z",
    }), 200


def _self_ping_loop(url: str):
    """Send a GET request to our own /ping URL every SELF_PING_INTERVAL seconds."""
    # Wait for Flask to finish starting before the first ping
    time.sleep(10)
    while True:
        try:
            resp = requests.get(url, timeout=10)
            logger.debug("Self-ping → %s %s", url, resp.status_code)
        except Exception as exc:
            logger.warning("Self-ping failed: %s", exc)
        time.sleep(SELF_PING_INTERVAL)


def keep_alive(movie_count_fn=None):
    """Start the Flask server and the self-ping loop in background daemon threads."""
    global _movie_count_fn
    if movie_count_fn is not None:
        _movie_count_fn = movie_count_fn

    # Flask server
    flask_thread = threading.Thread(target=_run, daemon=True)
    flask_thread.start()
    logger.info("Keep-alive server started on port 8080")

    # Self-ping locally — avoids SSL issues and keeps the process alive
    ping_url = "http://localhost:8080/ping"
    ping_thread = threading.Thread(target=_self_ping_loop, args=(ping_url,), daemon=True)
    ping_thread.start()
    logger.info("Self-ping started → %s (every %ds)", ping_url, SELF_PING_INTERVAL)


def _run():
    log = logging.getLogger("werkzeug")
    log.setLevel(logging.WARNING)
    _flask_app.run(host="0.0.0.0", port=8080, use_reloader=False)
