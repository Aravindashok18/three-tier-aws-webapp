# Three-Tier Web App on AWS

A small notes service deployed the way a production web app usually is: an **Application Load Balancer** in front of **Nginx + gunicorn on EC2** (web and app tiers), a **MySQL database on RDS** that the internet cannot reach (data tier), and a private **S3** bucket for static assets and releases, all inside a custom **VPC**. The whole thing is one CloudFormation template plus Bash scripts for bootstrapping, hardening, deploying and health-checking the hosts.

**Stack:** AWS (VPC, EC2 + Auto Scaling, ALB, RDS MySQL 8.4, S3, IAM, Secrets Manager) · Ubuntu 24.04 · Nginx · Bash · Python (Flask, SQLAlchemy, gunicorn) · CloudFormation

> **Status: written and tested locally, never deployed.** No AWS account was used to build this, so nothing here has run on a real EC2 instance, RDS database or load balancer. [What was and was not verified](#what-was-verified-and-what-was-not) lists exactly which parts were exercised and how.

## Architecture

```
                        Internet
                           │ 80 (and 443 if you give it a certificate)
                           ▼
        ┌──────────────────────────────────────────┐
        │  Application Load Balancer               │  public subnets, two AZs
        │  sg-alb: in 80/443 from anywhere,        │
        │          out 80 to sg-app only           │
        └──────────────────────┬───────────────────┘
                               │ 80, only from sg-alb
                               ▼
        ┌──────────────────────────────────────────┐
        │  EC2 x 2..4 in an Auto Scaling group     │  public subnets, two AZs
        │  Nginx :80  ──►  gunicorn 127.0.0.1:8000 │
        │  sg-app: in 80 from sg-alb, 22 from      │
        │          AdminCidr; out 80/443 + 3306    │
        └───────┬──────────────────────┬───────────┘
                │ 3306 over TLS,       │ HTTPS via the S3 gateway endpoint:
                │ only from sg-app     │ release bundle + static files
                ▼                      ▼
        ┌───────────────────┐   ┌──────────────────────────────┐
        │  RDS MySQL 8.4    │   │  S3 bucket                   │
        │  private subnets, │   │  all public access blocked,  │
        │  no internet route│   │  encrypted, versioned,       │
        │  sg-db: in 3306   │   │  HTTPS-only policy           │
        │  from sg-app only │   └──────────────────────────────┘
        └───────────────────┘
```

| Tier | Runs on | Who can reach it |
|---|---|---|
| Web | ALB, then Nginx on each instance | Anyone, on 80/443 (ALB only). Instances accept port 80 from the ALB's security group and nothing else. |
| App | gunicorn + Flask on `127.0.0.1:8000` | Nginx on the same host. Not bound to a network interface. |
| Data | RDS MySQL in two private subnets | The app security group, on 3306, over TLS. The private route table has no route to the internet, the instance is not publicly accessible, and the database security group has no outbound rules. |

Design choices worth knowing about:

- **The instances sit in public subnets**, with public IPs, so they can fetch packages and talk to S3 without a NAT gateway (which bills hourly). The security groups are what keep them closed: port 80 only from the load balancer, SSH only from `AdminCidr`. The trade-off is that a mistake in a security group exposes a host directly; a stricter variant would put the instances in private subnets behind a NAT gateway or VPC endpoints.
- **No database password exists in the template, the repo or on disk.** RDS generates it (`ManageMasterUserPassword`) and stores it in Secrets Manager. The instance role may read that one secret, and the app fetches it at start-up with boto3 (`app/config.py`). `/etc/webapp/env` holds only the secret's ARN.
- **The database refuses unencrypted connections** (`require_secure_transport=1`), and the app verifies the server certificate against the RDS CA bundle that `bootstrap.sh` downloads.
- **Instances only accept IMDSv2**, have encrypted disks, and their IAM role can read `releases/*` and `static/*` in one bucket and one secret. It has no wildcard actions or resources. `AmazonSSMManagedInstanceCore` is attached so you can use Session Manager instead of SSH: leave `AdminCidr` empty and no SSH port is opened at all.
- **Static files** live in S3 and are synced onto each instance (at boot and hourly by cron), then served by Nginx. The bucket stays private; there is no public bucket or CloudFront in this version.
- **The load balancer checks `/health`, which never touches the database**, so a database outage cannot pull every instance out of rotation at once. `/health/ready` does check the database, for dashboards and alarms.

## Repository layout

```
infra/three-tier.yaml        the whole stack: VPC, subnets, route tables, security groups, S3, RDS, IAM,
                             launch template, Auto Scaling group, ALB, listeners
app/                         Flask app (main.py routes, db.py all SQL, config.py DB credentials), templates, static files
config/                      Nginx site, systemd unit, sshd and fail2ban settings, logrotate, unattended-upgrades
scripts/
  bootstrap.sh               turns a fresh Ubuntu instance into an app server (run by EC2 user data)
  harden.sh                  key-only SSH, default-deny UFW, fail2ban, automatic security updates
  healthcheck.sh             cron job: restarts Nginx or the app when either stops answering
  audit-ports.sh             lists the ports that accept network connections, before and after hardening
  deploy.sh                  packages the app, uploads it and the static files to S3, rolls the instances
  lib.sh                     shared helpers, including DRY_RUN mode
tests/                       103 tests, see below
```

## The host scripts

**`healthcheck.sh`** runs from cron every 5 minutes (`/etc/cron.d/webapp-maintenance`). For `webapp` and then `nginx` it checks both that systemd reports the unit active *and* that it answers an HTTP request, because a process can be "active" and wedged. A failing service is restarted and re-checked after a few seconds. A service that has already been restarted three times in the last hour is left alone and the script exits 1, since restarting forever hides a real fault. A lock directory stops overlapping runs; stale locks from a crashed run are cleared. Everything is logged to `/var/log/webapp-health.log` and syslog.

**`harden.sh`** installs an `sshd_config.d/00-` drop-in (`PasswordAuthentication no`, `PermitRootLogin no`, `MaxAuthTries 3`, no forwarding), validates it with `sshd -t` before reloading and removes it if sshd rejects it, sets UFW to deny incoming by default and allows only SSH from `ADMIN_CIDR` and port 80 from the VPC, configures a fail2ban `sshd` jail (4 failures in 10 minutes bans the address for an hour) and turns on unattended security upgrades. It is idempotent. The `00-` prefix matters: sshd keeps the *first* value it reads, and cloud images ship their own files in that directory.

**`audit-ports.sh`** counts TCP/UDP ports listening on a non-loopback address, so the effect of hardening can be measured on a real host:

```bash
sudo scripts/audit-ports.sh --count     # before
sudo VPC_CIDR=10.0.0.0/16 ADMIN_CIDR=203.0.113.7/32 scripts/harden.sh
sudo scripts/audit-ports.sh --count     # after
```

Every script supports `DRY_RUN=1`, which prints each command and file instead of applying it: `DRY_RUN=1 VPC_CIDR=10.0.0.0/16 scripts/harden.sh`.

## Deploying

Needs an AWS account, the AWS CLI and (for SSH) an EC2 key pair. This creates billable resources (ALB, RDS, EC2).

```bash
# 1. Create the stack. Instances start, then wait up to 30 minutes for the release bundle.
aws cloudformation deploy --stack-name notes --template-file infra/three-tier.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides AdminCidr=203.0.113.7/32 KeyName=my-key     # or leave both empty and use Session Manager

# 2. Upload the app and static files; rolls instances if any are already running.
scripts/deploy.sh notes

# 3. Open the URL it prints. Reload a few times: the "Served by" host name changes between instances.
```

Parameters: `AdminCidr` (rejected if it is `0.0.0.0/0`), `KeyName`, `InstanceType`, `MinSize`/`MaxSize`, `DBInstanceClass`, `DBMultiAZ`, `CertificateArn` (adds the 443 listener and redirects HTTP to it). Later deploys are just `scripts/deploy.sh notes`: new bundle to S3, then an instance refresh that keeps at least half the instances serving.

Teardown: `aws cloudformation delete-stack --stack-name notes`. RDS takes a final snapshot, and the S3 bucket is retained on purpose (it holds releases); empty and delete it yourself.

## Tests

```bash
python3.12 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest                       # 81 tests; the 9 integration and 13 MySQL tests skip without nginx / MySQL
NGINX_BIN=/path/to/nginx .venv/bin/python -m pytest   # adds the 9 integration tests
```

| File | Tests | What it checks |
|---|---|---|
| `test_app.py` | 22 | Notes API (create, list, delete, validation, SQL metacharacters stored as text, output escaping), liveness vs readiness, schema creation that is safe to run twice and retries while the database starts, database URL building from Secrets Manager (with a stub client) including passwords with URL-special characters, TLS options for MySQL only. |
| `test_template.py` | 24 | `cfn-lint` finds nothing. The security design as assertions: database private, encrypted and without a password in the template; private subnets have no internet route; MySQL reachable only from the app group; only the load balancer is open to the world; no SSH from anywhere; bucket private, encrypted, versioned and HTTPS-only; IMDSv2; no wildcard IAM; and the contracts between the template, `bootstrap.sh`, `deploy.sh` and the Nginx config. I checked these tests catch real mistakes by breaking the template six ways (database made public, app opened to the internet, bucket ACL unblocked, IMDSv1 allowed, `s3:*` added, an internet route added to a private subnet): each was caught. |
| `test_scripts.py` | 35 | `healthcheck.sh` and `deploy.sh` run for real against stub `systemctl`, `curl` and `aws` programs (restart decisions, the hourly limit, locking, bundle contents). `harden.sh` and `bootstrap.sh` in `DRY_RUN` mode (order of steps, firewall rules before enabling UFW, no password written to disk). The real `sshd -t` accepts the hardening file. |
| `test_integration.py` | 9 | Real Nginx 1.31 in front of real gunicorn on one machine, using the rendered production Nginx config (only the listen port and upstream port are swapped). Requests flow through, static files are served by Nginx with the security headers intact, `/nginx-health` keeps answering when the app is dead, and the real `healthcheck.sh` brings back a killed gunicorn and a killed Nginx. |
| `test_mysql.py` | 13 | The app against a real MySQL server, run only when `WEBAPP_MYSQL_ADMIN_URL` (and for TLS, `WEBAPP_MYSQL_TLS_URL`, `_TLS_CA`, `_WRONG_CA`; see the file header) point at scratch servers. The schema and column types; creating it twice and from 8 instances at once; notes CRUD, emoji and non-Latin text (utf8mb4), maximum-length notes, SQL metacharacters; a password full of URL-special characters coming through the Secrets Manager path; the `caching_sha2_password` login. With a second server started with `require_secure_transport=ON`, like the RDS parameter group: the app connects over TLS when given the CA, a plain connection is refused, and a certificate from another CA is rejected. I checked the TLS and concurrency tests by breaking the app (ignoring the CA option; using check-then-create for the schema): both were caught. |

The integration tests found one real bug while I was writing them: an `add_header` line inside `location /static/` makes Nginx stop inheriting the server-level security headers, so static responses lost `X-Content-Type-Options` and `X-Frame-Options` (and `Cache-Control` was sent twice). The location now uses `expires` alone, and a test asserts the headers.

## What was verified, and what was not

**Verified on a Mac, with the tests above:** the app and its SQL on SQLite and on a real MySQL server (version 9.7.2, a local scratch build: plain and TLS-only), the template's schema (cfn-lint) and security properties, the Bash scripts' logic against stubs, the sshd settings against a real `sshd`, and the Nginx → gunicorn → Flask path including crash recovery.

**Not verified, because it needs AWS or an Ubuntu server:**

- The stack has never been created. cfn-lint checks the template against CloudFormation's schemas, not against what a region offers, so things like the RDS engine version (`8.4.11`), the Canonical AMI parameter and the instance profile ordering are untested.
- `bootstrap.sh` has never run on Ubuntu 24.04: package names (`awscli`, `unattended-upgrades`), `apt`, `systemd` sandboxing in `webapp.service`, and the RDS CA download are unexercised.
- UFW and fail2ban were never run, so there is no before/after port count. The scripts configure them and `audit-ports.sh` can measure it; I have no number for how many ports a stock Ubuntu image exposes versus a hardened one, and this README does not claim one.
- **MySQL 8.4 and RDS itself.** The MySQL tests ran on 9.7.2, not the 8.4 LTS release the template asks for, and TLS was verified against a certificate from a throwaway CA, not RDS's real CA bundle (`bootstrap.sh` downloads that bundle; this was not run). The `username`/`password` keys the app reads from the RDS-managed secret follow AWS's documented format but were not read from a live secret.
- Load balancer health checks, Auto Scaling rolling updates, instance refresh and failover are untested. Nothing was load-tested.

**Known limits:** no HTTPS unless you supply an ACM certificate; no CloudFront; no alarms or dashboards (CloudWatch logs for RDS are exported, nothing else is wired up); the Auto Scaling group has no scheduled scaling; the app has no authentication, which is fine for a demo notes board and not for anything private.
