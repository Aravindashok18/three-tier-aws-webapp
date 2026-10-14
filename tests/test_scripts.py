"""Tests for the Bash scripts.

None of them needs an Ubuntu server. healthcheck.sh and deploy.sh run for real
against stub `systemctl`, `curl` and `aws` programs that record how they were
called. harden.sh and bootstrap.sh run in DRY_RUN mode, which prints every command
and file they would write.
"""
import os
import shutil
import subprocess
import tarfile
import textwrap
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"


def sh(script, *args, env=None, cwd=None):
    environment = {**os.environ, **(env or {})}
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True, env=environment, cwd=cwd)


def write_stub(directory, name, body):
    path = directory / name
    path.write_text("#!/bin/bash\n" + textwrap.dedent(body))
    path.chmod(0o755)


# --- healthcheck.sh -----------------------------------------------------------------------

class FakeHost:
    """Stub systemctl/curl/logger/sleep whose answers come from marker files in state/."""

    def __init__(self, tmp_path):
        self.bin = tmp_path / "bin"
        self.state = tmp_path / "state"
        self.health_dir = tmp_path / "health"
        self.log = tmp_path / "calls.log"
        for folder in (self.bin, self.state):
            folder.mkdir()
        self.log.write_text("")
        write_stub(self.bin, "systemctl", """
            echo "systemctl $*" >> "$STUB_LOG"
            case "$1" in
              is-active) [ -f "$STUB_STATE/$3.active" ] ;;
              restart)
                if [ -f "$STUB_STATE/$2.heals" ]; then touch "$STUB_STATE/$2.active" "$STUB_STATE/$2.http"; fi
                [ ! -f "$STUB_STATE/$2.restart_fails" ] ;;
            esac
        """)
        write_stub(self.bin, "curl", """
            echo "curl $*" >> "$STUB_LOG"
            url="${!#}"
            case "$url" in *:8000*) svc=webapp ;; *nginx-health*) svc=nginx ;; esac
            [ -f "$STUB_STATE/$svc.http" ]
        """)
        write_stub(self.bin, "logger", 'echo "logger $*" >> "$STUB_LOG"\n')
        write_stub(self.bin, "sleep", 'echo "sleep $*" >> "$STUB_LOG"\n')
        for service in ("webapp", "nginx"):
            self.set(service)

    def set(self, service, active=True, http=True, heals=True, restart_fails=False):
        for suffix, on in (("active", active), ("http", http), ("heals", heals), ("restart_fails", restart_fails)):
            marker = self.state / f"{service}.{suffix}"
            marker.touch() if on else marker.unlink(missing_ok=True)

    def run(self, **env):
        return sh(SCRIPTS / "healthcheck.sh", env={
            "PATH": f"{self.bin}:{os.environ['PATH']}",
            "STUB_LOG": str(self.log), "STUB_STATE": str(self.state),
            "STATE_DIR": str(self.health_dir), "SETTLE_SECONDS": "0", **env,
        })

    def calls(self):
        return self.log.read_text().splitlines()

    def restarts(self):
        return [c for c in self.calls() if c.startswith("systemctl restart")]


@pytest.fixture()
def host(tmp_path):
    return FakeHost(tmp_path)


def test_healthy_host_is_left_alone(host):
    result = host.run()

    assert result.returncode == 0
    assert host.restarts() == []
    assert "UNHEALTHY" not in result.stdout


def test_stopped_app_is_restarted(host):
    host.set("webapp", active=False, http=False)

    result = host.run()

    assert result.returncode == 0
    assert host.restarts() == ["systemctl restart webapp"]
    assert "RECOVERED webapp" in result.stdout


def test_app_that_is_active_but_not_answering_is_restarted(host):
    host.set("webapp", active=True, http=False)

    result = host.run()

    assert host.restarts() == ["systemctl restart webapp"]
    assert "RECOVERED webapp" in result.stdout
    assert result.returncode == 0


def test_only_the_broken_service_is_restarted(host):
    host.set("nginx", active=False, http=False)

    host.run()

    assert host.restarts() == ["systemctl restart nginx"]


def test_both_services_down_restarts_the_app_first(host):
    host.set("webapp", active=False, http=False)
    host.set("nginx", active=False, http=False)

    host.run()

    assert host.restarts() == ["systemctl restart webapp", "systemctl restart nginx"]


def test_service_that_stays_broken_after_restart_fails_the_run(host):
    host.set("webapp", active=False, http=False, heals=False)

    result = host.run()

    assert result.returncode == 1
    assert "STILL UNHEALTHY webapp" in result.stdout


def test_failed_restart_is_reported(host):
    host.set("webapp", active=False, http=False, heals=False, restart_fails=True)

    result = host.run()

    assert result.returncode == 1
    assert "FAILED to restart webapp" in result.stdout


def test_stops_restarting_after_the_hourly_limit(host):
    host.set("webapp", active=False, http=False)
    host.health_dir.mkdir()
    now = int(time.time())
    (host.health_dir / "webapp.restarts").write_text("".join(f"{now - 60 * n}\n" for n in (5, 10, 20)))

    result = host.run(MAX_RESTARTS_PER_HOUR="3")

    assert result.returncode == 1
    assert host.restarts() == []
    assert "GIVING UP on webapp" in result.stdout


def test_restarts_older_than_an_hour_do_not_count(host):
    host.set("webapp", active=False, http=False)
    host.health_dir.mkdir()
    now = int(time.time())
    (host.health_dir / "webapp.restarts").write_text("".join(f"{now - 4000 - n}\n" for n in range(5)))

    result = host.run(MAX_RESTARTS_PER_HOUR="3")

    assert result.returncode == 0
    assert host.restarts() == ["systemctl restart webapp"]
    assert len((host.health_dir / "webapp.restarts").read_text().split()) == 1  # old entries were pruned


def test_a_second_run_is_skipped_while_the_first_is_still_going(host):
    host.set("webapp", active=False, http=False)
    (host.health_dir / "lock").mkdir(parents=True)
    (host.health_dir / "lock" / "pid").write_text(str(os.getpid()))  # a process that is certainly alive

    result = host.run()

    assert result.returncode == 0
    assert "still running" in result.stdout
    assert host.restarts() == []


def test_a_lock_left_by_a_dead_run_is_ignored(host):
    host.set("webapp", active=False, http=False)
    (host.health_dir / "lock").mkdir(parents=True)
    dead = subprocess.Popen(["true"])
    dead.wait()
    (host.health_dir / "lock" / "pid").write_text(str(dead.pid))

    host.run()

    assert host.restarts() == ["systemctl restart webapp"]
    assert not (host.health_dir / "lock").exists()  # released on exit


def test_events_go_to_syslog_as_well_as_stdout(host):
    host.set("webapp", active=False, http=False)

    host.run()

    assert any(c.startswith("logger -t webapp-health") and "UNHEALTHY webapp" in c for c in host.calls())


# --- audit-ports.sh -----------------------------------------------------------------------

def test_audit_counts_each_exposed_port_once_and_ignores_loopback(tmp_path):
    write_stub(tmp_path, "ss", """
        cat <<'OUT'
        udp   UNCONN 0 0    127.0.0.53%lo:53    0.0.0.0:*
        udp   UNCONN 0 0    10.0.1.5%eth0:68    0.0.0.0:*
        tcp   LISTEN 0 4096 0.0.0.0:22          0.0.0.0:*
        tcp   LISTEN 0 4096 [::]:22             [::]:*
        tcp   LISTEN 0 511  0.0.0.0:80          0.0.0.0:*
        tcp   LISTEN 0 511  [::]:80             [::]:*
        tcp   LISTEN 0 2048 127.0.0.1:8000      0.0.0.0:*
        tcp   LISTEN 0 100  [::1]:25            [::]:*
        OUT
    """)
    path = f"{tmp_path}:{os.environ['PATH']}"

    counted = sh(SCRIPTS / "audit-ports.sh", "--count", env={"PATH": path})
    listed = sh(SCRIPTS / "audit-ports.sh", env={"PATH": path})

    assert counted.stdout.strip() == "3"
    assert "tcp/22" in listed.stdout and "tcp/80" in listed.stdout and "udp/68" in listed.stdout
    assert "8000" not in listed.stdout and "udp/53" not in listed.stdout


def test_audit_reports_zero_when_nothing_listens(tmp_path):
    write_stub(tmp_path, "ss", "true\n")

    result = sh(SCRIPTS / "audit-ports.sh", "--count", env={"PATH": f"{tmp_path}:{os.environ['PATH']}"})

    assert result.stdout.strip() == "0"


# --- harden.sh ---------------------------------------------------------------------------

def harden(tmp_path, **env):
    keys = tmp_path / "authorized_keys"
    keys.write_text("ssh-ed25519 AAAA test\n")
    base = {"DRY_RUN": "1", "VPC_CIDR": "10.0.0.0/16", "ADMIN_CIDR": "203.0.113.7/32", "AUTH_KEYS_FILE": str(keys)}
    return sh(SCRIPTS / "harden.sh", env={**base, **env})


def test_firewall_denies_by_default_and_opens_only_ssh_and_http(tmp_path):
    out = harden(tmp_path).stdout

    assert "DRY-RUN: ufw default deny incoming" in out
    assert "DRY-RUN: ufw allow from 203.0.113.7/32 to any port 22 proto tcp" in out
    assert "DRY-RUN: ufw allow from 10.0.0.0/16 to any port 80 proto tcp" in out
    assert out.count("ufw allow from") == 2
    # the SSH rule must exist before the firewall is switched on, or enabling it would cut the session
    assert out.index("port 22") < out.index("ufw --force enable")


def test_ssh_stays_closed_without_an_admin_address(tmp_path):
    out = harden(tmp_path, ADMIN_CIDR="").stdout

    assert "port 22" not in out
    assert "port 80" in out


def test_sshd_drop_in_sorts_first_and_disables_passwords(tmp_path):
    out = harden(tmp_path).stdout

    assert "/etc/ssh/sshd_config.d/00-webapp-hardening.conf" in out
    for line in ("PasswordAuthentication no", "KbdInteractiveAuthentication no", "PermitRootLogin no", "MaxAuthTries 3"):
        assert f"| {line}" in out
    assert out.index("00-webapp-hardening.conf") < out.index("systemctl reload ssh")


def test_fail2ban_ignores_the_vpc_and_watches_ssh(tmp_path):
    out = harden(tmp_path, VPC_CIDR="172.16.0.0/20").stdout

    assert "| ignoreip = 127.0.0.1/8 ::1 172.16.0.0/20" in out
    assert "| [sshd]" in out and "| enabled = true" in out
    assert "__VPC_CIDR__" not in out
    assert out.index("00-webapp.local") < out.index("systemctl restart fail2ban")


def test_missing_ssh_keys_warn_but_do_not_abort(tmp_path):
    result = harden(tmp_path, AUTH_KEYS_FILE=str(tmp_path / "nope"))

    assert result.returncode == 0
    assert "warning" in result.stdout and "Session Manager" in result.stdout


def test_harden_requires_the_vpc_range(tmp_path):
    result = harden(tmp_path, VPC_CIDR="")

    assert result.returncode != 0
    assert "VPC_CIDR" in result.stderr


@pytest.mark.skipif(not (shutil.which("sshd") or Path("/usr/sbin/sshd").exists()), reason="sshd is not installed")
def test_the_real_sshd_accepts_the_hardening_file(tmp_path):
    sshd = shutil.which("sshd") or "/usr/sbin/sshd"
    host_key = tmp_path / "host_key"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(host_key)], check=True)
    config = tmp_path / "sshd_config"
    config.write_text(f"HostKey {host_key}\n" + (ROOT / "config" / "sshd-hardening.conf").read_text())

    result = subprocess.run([sshd, "-t", "-f", str(config)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


# --- bootstrap.sh -------------------------------------------------------------------------

BOOTSTRAP_ENV = {
    "DRY_RUN": "1", "APP_BUCKET": "my-bucket", "AWS_REGION": "eu-west-1", "DB_HOST": "db.abc.eu-west-1.rds.amazonaws.com",
    "DB_SECRET_ARN": "arn:aws:secretsmanager:eu-west-1:123456789012:secret:rds-AbCd", "VPC_CIDR": "10.0.0.0/16",
}


def bootstrap(**env):
    return sh(SCRIPTS / "bootstrap.sh", env={**BOOTSTRAP_ENV, **env})


def test_bootstrap_runs_its_steps_in_a_safe_order():
    out = bootstrap().stdout
    order = [
        "apt-get install", "python3 -m venv", "pip install", "/etc/webapp/env", "aws s3 sync",
        "systemctl enable --now webapp", "nginx -t", "systemctl restart nginx", "/etc/cron.d/webapp-maintenance", "harden.sh",
    ]

    positions = [out.index(step) for step in order]

    assert positions == sorted(positions), dict(zip(order, positions))


def test_bootstrap_never_writes_the_database_password_to_disk():
    out = bootstrap().stdout

    assert "| DB_SECRET_ARN=arn:aws:secretsmanager" in out
    assert "PASSWORD" not in out.upper().replace("DB_SECRET_ARN", "")
    assert "| DB_SSL_CA=/etc/webapp/rds-ca.pem" in out


def test_bootstrap_renders_the_nginx_config_without_leftover_placeholders():
    out = bootstrap().stdout

    assert "alias /opt/webapp/static/;" in out
    assert "set_real_ip_from 10.0.0.0/16;" in out
    assert "__" not in out.split("write /etc/nginx/sites-available/webapp")[1].split("DRY-RUN: ln")[0]


def test_bootstrap_schedules_the_health_check_every_five_minutes():
    out = bootstrap().stdout

    assert "| */5 * * * * root /opt/webapp/current/scripts/healthcheck.sh >> /var/log/webapp-health.log 2>&1" in out


def test_bootstrap_passes_the_network_settings_to_the_hardening_step():
    out = bootstrap(ADMIN_CIDR="198.51.100.9/32").stdout

    assert "env VPC_CIDR=10.0.0.0/16 ADMIN_CIDR=198.51.100.9/32" in out


@pytest.mark.parametrize("missing", ["APP_BUCKET", "DB_HOST", "DB_SECRET_ARN", "VPC_CIDR", "AWS_REGION"])
def test_bootstrap_refuses_to_start_without_its_settings(missing):
    result = sh(SCRIPTS / "bootstrap.sh", env={**BOOTSTRAP_ENV, missing: ""})

    assert result.returncode != 0
    assert missing in result.stderr


# --- deploy.sh ----------------------------------------------------------------------------

@pytest.fixture()
def fake_aws(tmp_path):
    bin_dir = tmp_path / "awsbin"
    bin_dir.mkdir()
    log = tmp_path / "aws.log"
    log.write_text("")
    write_stub(bin_dir, "aws", """
        echo "$*" >> "$AWS_LOG"
        case "$*" in
          *AppBucketName*) echo my-bucket ;;
          *AutoScalingGroupName*) echo my-asg ;;
          *LoadBalancerUrl*) echo http://alb.example.com ;;
          *describe-auto-scaling-groups*) echo "$AWS_IN_SERVICE" ;;
        esac
    """)
    return bin_dir, log


def deploy(fake_aws, in_service, label="test1"):
    bin_dir, log = fake_aws
    env = {"PATH": f"{bin_dir}:{os.environ['PATH']}", "AWS_LOG": str(log), "AWS_IN_SERVICE": str(in_service)}
    result = sh(SCRIPTS / "deploy.sh", "my-stack", label, env=env)
    return result, log.read_text().splitlines()


def test_deploy_uploads_release_and_static_files_then_refreshes_running_instances(fake_aws):
    try:
        result, calls = deploy(fake_aws, in_service=2)

        assert result.returncode == 0, result.stderr
        assert any(c.startswith("s3 cp") and c.endswith("s3://my-bucket/releases/webapp-test1.tgz --only-show-errors") for c in calls)
        assert any(c.startswith("s3 cp") and "s3://my-bucket/releases/webapp-latest.tgz" in c for c in calls)
        assert any(c.startswith("s3 sync") and "app/static s3://my-bucket/static --delete" in c for c in calls)
        refresh = [c for c in calls if "start-instance-refresh" in c]
        assert len(refresh) == 1 and "my-asg" in refresh[0] and "MinHealthyPercentage=50" in refresh[0]
        assert "http://alb.example.com" in result.stdout
    finally:
        shutil.rmtree(ROOT / "dist", ignore_errors=True)


def test_first_deploy_has_nothing_to_refresh(fake_aws):
    try:
        result, calls = deploy(fake_aws, in_service=0)

        assert result.returncode == 0, result.stderr
        assert not any("start-instance-refresh" in c for c in calls)
        assert "new ones pick the release up" in result.stdout
    finally:
        shutil.rmtree(ROOT / "dist", ignore_errors=True)


def test_release_bundle_contains_what_the_instance_needs_and_nothing_else(fake_aws):
    try:
        deploy(fake_aws, in_service=0)
        with tarfile.open(ROOT / "dist" / "webapp-test1.tgz") as bundle:
            names = bundle.getnames()
    finally:
        shutil.rmtree(ROOT / "dist", ignore_errors=True)

    for needed in ("app/main.py", "app/static/css/site.css", "scripts/bootstrap.sh", "scripts/healthcheck.sh",
                   "config/webapp.service", "config/nginx-webapp.conf.template", "requirements.txt"):
        assert needed in names, needed
    assert not [n for n in names if n.startswith(("tests", ".venv", ".git", "infra", "dist"))]
    assert not [n for n in names if "__pycache__" in n or n.endswith(".pyc") or "/._" in n]


def test_deploy_stops_if_the_stack_has_no_bucket(fake_aws):
    bin_dir, log = fake_aws
    write_stub(bin_dir, "aws", 'echo None\n')

    result = sh(SCRIPTS / "deploy.sh", "my-stack", "x", env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})

    assert result.returncode != 0
    assert "no AppBucketName" in result.stderr
