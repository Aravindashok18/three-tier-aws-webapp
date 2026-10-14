"""Nginx in front of gunicorn in front of Flask, on one machine.

This is the closest thing to the EC2 instance that runs without AWS: the real
rendered Nginx config, the real gunicorn command line, the real app on SQLite, and
the real healthcheck.sh with only `systemctl` replaced by a stub that starts the
process directly. Skipped when no nginx binary is available (set NGINX_BIN, or
install nginx).
"""
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import textwrap
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
NGINX = os.environ.get("NGINX_BIN") or shutil.which("nginx")
GUNICORN = Path(sys.executable).parent / "gunicorn"

pytestmark = pytest.mark.skipif(not NGINX, reason="nginx is not installed (set NGINX_BIN to run the integration tests)")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fetch(url, method="GET", body=None, timeout=5):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read().decode()
    except urllib.error.HTTPError as error:
        return error.code, dict(error.headers), error.read().decode()


def wait_until(check, seconds=15):
    deadline = time.time() + seconds
    while time.time() < deadline:
        try:
            if check():
                return True
        except Exception:
            pass
        time.sleep(0.2)
    return False


class Stack:
    def __init__(self, tmp_path):
        self.dir = tmp_path
        self.web_port, self.app_port = free_port(), free_port()
        self.web = f"http://127.0.0.1:{self.web_port}"
        self.procs = {}
        (tmp_path / "static" / "css").mkdir(parents=True)
        (tmp_path / "static" / "css" / "site.css").write_text("body { margin: 0 }\n")
        self._write_nginx_conf()
        self.env = {**os.environ, "DATABASE_URL": f"sqlite:///{tmp_path}/notes.db"}

    def _write_nginx_conf(self):
        rendered = self.dir / "webapp.conf"
        # the same render() that bootstrap.sh uses
        subprocess.run(
            ["bash", "-c", f'. "{ROOT}/scripts/lib.sh"; render "{ROOT}/config/nginx-webapp.conf.template" "{rendered}" 644 '
                           f'"APP_DIR={self.dir}" "VPC_CIDR=10.0.0.0/16"'],
            check=True,
        )
        text = rendered.read_text()
        assert "__APP_DIR__" not in text and "__VPC_CIDR__" not in text
        # Only these two lines differ from production: an unprivileged port, and the app's port.
        text = text.replace("listen 80 default_server;", f"listen 127.0.0.1:{self.web_port};")
        text = text.replace("server 127.0.0.1:8000;", f"server 127.0.0.1:{self.app_port};")
        rendered.write_text(text)
        (self.dir / "nginx.conf").write_text(textwrap.dedent(f"""\
            daemon off;
            pid {self.dir}/nginx.pid;
            error_log {self.dir}/error.log info;
            events {{ worker_connections 64; }}
            http {{
                access_log {self.dir}/access.log;
                include {rendered};
            }}
        """))

    def nginx_args(self):
        return [NGINX, "-p", str(self.dir), "-c", str(self.dir / "nginx.conf")]

    def start_app(self):
        self.procs["app"] = subprocess.Popen(
            [str(GUNICORN), "--workers", "2", "--bind", f"127.0.0.1:{self.app_port}", "app.wsgi:app"],
            cwd=ROOT, env=self.env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True,
        )
        assert wait_until(lambda: fetch(f"http://127.0.0.1:{self.app_port}/health")[0] == 200), "gunicorn did not start"

    def start_nginx(self):
        self.procs["nginx"] = subprocess.Popen(
            self.nginx_args(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        assert wait_until(lambda: fetch(f"{self.web}/nginx-health")[0] == 200), (self.dir / "error.log").read_text()

    def kill(self, name):
        proc = self.procs.pop(name)
        os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()

    def stop_all(self):
        for name in list(self.procs):
            self.kill(name)


@pytest.fixture()
def stack(tmp_path):
    stack = Stack(tmp_path)
    stack.start_app()
    stack.start_nginx()
    yield stack
    stack.stop_all()


def test_nginx_accepts_the_rendered_config(tmp_path):
    result = subprocess.run([*Stack(tmp_path).nginx_args(), "-t"], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr
    assert "syntax is ok" in result.stderr


def test_requests_reach_the_app_through_nginx(stack):
    status, headers, body = fetch(f"{stack.web}/health")

    assert status == 200
    assert json.loads(body)["status"] == "ok"
    assert headers["X-Served-By"]  # set by Flask, so the request really went through to the app


def test_a_note_posted_through_nginx_is_stored_and_rendered(stack):
    status, _, body = fetch(f"{stack.web}/api/notes", "POST", {"author": "Asha", "body": "via nginx"})
    assert status == 201

    _, _, page = fetch(f"{stack.web}/")
    assert "via nginx" in page and "Asha" in page
    assert fetch(f"{stack.web}/api/notes/{json.loads(body)['id']}", "DELETE")[0] == 204


def test_static_files_are_served_by_nginx_itself(stack):
    status, headers, body = fetch(f"{stack.web}/static/css/site.css")

    assert status == 200 and "margin: 0" in body
    assert headers["Cache-Control"] == "max-age=3600"
    assert "Expires" in headers
    assert "X-Served-By" not in headers  # Flask never saw it
    # a location with its own add_header would silently drop these server-level headers
    assert headers["X-Content-Type-Options"] == "nosniff" and headers["X-Frame-Options"] == "DENY"

    (stack.dir / "static" / "css" / "site.css").unlink()
    status, headers, _ = fetch(f"{stack.web}/static/css/site.css")
    assert status == 404 and "X-Served-By" not in headers  # a missing asset is not passed on to the app


def test_security_headers_and_no_version_leak(stack):
    _, headers, _ = fetch(f"{stack.web}/health")

    assert headers["X-Frame-Options"] == "DENY"
    assert headers["X-Content-Type-Options"] == "nosniff"
    assert headers["Server"] == "nginx"  # no version number


def test_nginx_health_still_answers_when_the_app_is_dead(stack):
    stack.kill("app")

    assert fetch(f"{stack.web}/nginx-health")[0] == 200
    assert fetch(f"{stack.web}/health")[0] == 502


# --- the real healthcheck.sh against live processes ------------------------------------------

def run_healthcheck(stack):
    bin_dir = stack.dir / "bin"
    bin_dir.mkdir(exist_ok=True)
    restarts = stack.dir / "restarts.log"
    nginx_cmd = " ".join(f'"{a}"' for a in stack.nginx_args())
    stub = bin_dir / "systemctl"
    stub.write_text(textwrap.dedent(f"""\
        #!/bin/bash
        # Stand-in for systemd: "restart" starts the process the way the unit file would, then returns.
        # The redirections are on the whole group so the caller's pipes are not held open by the daemon.
        case "$1" in
          is-active) exit 0 ;;
          restart)
            echo "$2" >> "{restarts}"
            case "$2" in
              webapp) ( cd "{ROOT}" && DATABASE_URL="sqlite:///{stack.dir}/notes.db" exec "{GUNICORN}" --workers 2 --bind 127.0.0.1:{stack.app_port} app.wsgi:app ) >/dev/null 2>&1 & ;;
              nginx) ( exec {nginx_cmd} ) >/dev/null 2>&1 & ;;
            esac ;;
        esac
    """))
    stub.chmod(0o755)
    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "healthcheck.sh")], capture_output=True, text=True,
        env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "STATE_DIR": str(stack.dir / "health"),
             "APP_URL": f"http://127.0.0.1:{stack.app_port}/health", "NGINX_URL": f"{stack.web}/nginx-health",
             "SETTLE_SECONDS": "4"},
    )
    restarted = restarts.read_text().split() if restarts.exists() else []
    return result, restarted


def test_healthcheck_leaves_a_working_stack_alone(stack):
    result, restarted = run_healthcheck(stack)

    assert result.returncode == 0, result.stdout
    assert restarted == []


def test_healthcheck_brings_back_a_dead_app(stack):
    stack.kill("app")
    assert fetch(f"{stack.web}/health")[0] == 502

    result, restarted = run_healthcheck(stack)

    assert restarted == ["webapp"], result.stdout
    assert "RECOVERED webapp" in result.stdout
    assert fetch(f"{stack.web}/health")[0] == 200
    subprocess.run(["pkill", "-f", f"127.0.0.1:{stack.app_port}"])  # the stub's gunicorn is outside stack.procs


def test_healthcheck_brings_back_a_dead_nginx(stack):
    stack.kill("nginx")
    with pytest.raises(Exception):
        fetch(f"{stack.web}/nginx-health", timeout=2)

    result, restarted = run_healthcheck(stack)

    assert restarted == ["nginx"], result.stdout
    assert "RECOVERED nginx" in result.stdout
    assert fetch(f"{stack.web}/health")[0] == 200
    subprocess.run(["pkill", "-f", str(stack.dir / "nginx.conf")])
