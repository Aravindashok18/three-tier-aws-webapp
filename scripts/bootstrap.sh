#!/usr/bin/env bash
# Turns a fresh Ubuntu 24.04 instance into an app server. EC2 user data runs it as
# root after unpacking the release bundle; see infra/three-tier.yaml.
#
#   APP_BUCKET, AWS_REGION, DB_HOST, DB_SECRET_ARN, VPC_CIDR   required
#   DB_NAME (webapp), ADMIN_CIDR (empty)                        optional
#   DRY_RUN=1   print the commands and files instead of applying them
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RELEASE_SRC="$(cd "$HERE/.." && pwd)"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

: "${APP_BUCKET:?}" "${AWS_REGION:?}" "${DB_HOST:?}" "${DB_SECRET_ARN:?}" "${VPC_CIDR:?}"
DB_NAME="${DB_NAME:-webapp}"
ADMIN_CIDR="${ADMIN_CIDR:-}"
APP_DIR=/opt/webapp
RDS_CA=/etc/webapp/rds-ca.pem

require_root

log "1/8 packages"
export DEBIAN_FRONTEND=noninteractive
run apt-get update -y
run apt-get install -y nginx python3-venv python3-pip fail2ban ufw unattended-upgrades curl cron awscli

log "2/8 service account"
if ! id webapp >/dev/null 2>&1; then
  run useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin webapp
fi

log "3/8 application"
run mkdir -p "$APP_DIR"
run rm -rf "$APP_DIR/current"
run cp -a "$RELEASE_SRC" "$APP_DIR/current"
run python3 -m venv "$APP_DIR/venv"
run "$APP_DIR/venv/bin/pip" install --no-cache-dir -r "$APP_DIR/current/requirements.txt"

log "4/8 configuration"
# No password here: the app fetches it from Secrets Manager with the instance role.
# systemd reads this file as root, so it stays root-only.
write_file /etc/webapp/env 640 <<ENVFILE
DB_HOST=$DB_HOST
DB_NAME=$DB_NAME
DB_SECRET_ARN=$DB_SECRET_ARN
DB_SSL_CA=$RDS_CA
AWS_REGION=$AWS_REGION
ENVFILE
# RDS is configured to refuse unencrypted connections, so the app needs the RDS CA to verify it.
run curl -fsSL -o "$RDS_CA" https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem
run chmod 644 "$RDS_CA"

log "5/8 static assets from S3"
run mkdir -p "$APP_DIR/static"
run aws s3 sync "s3://$APP_BUCKET/static" "$APP_DIR/static" --delete --region "$AWS_REGION" --only-show-errors

log "6/8 app service"
run cp "$APP_DIR/current/config/webapp.service" /etc/systemd/system/webapp.service
run systemctl daemon-reload
run systemctl enable --now webapp

log "7/8 nginx"
render "$HERE/../config/nginx-webapp.conf.template" /etc/nginx/sites-available/webapp 644 \
  "APP_DIR=$APP_DIR" "VPC_CIDR=$VPC_CIDR"
run ln -sf /etc/nginx/sites-available/webapp /etc/nginx/sites-enabled/webapp
run rm -f /etc/nginx/sites-enabled/default
run nginx -t
run systemctl enable nginx
run systemctl restart nginx

log "8/8 scheduled jobs, then hardening"
write_file /etc/cron.d/webapp-maintenance 644 <<CRON
SHELL=/bin/bash
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
# restart Nginx or the app if either stops answering
*/5 * * * * root $APP_DIR/current/scripts/healthcheck.sh >> /var/log/webapp-health.log 2>&1
# pick up static assets that deploy.sh pushed to S3
17 * * * * root aws s3 sync s3://$APP_BUCKET/static $APP_DIR/static --delete --region $AWS_REGION --only-show-errors
CRON
run cp "$APP_DIR/current/config/logrotate-webapp-health" /etc/logrotate.d/webapp-health
run chmod +x "$APP_DIR/current/scripts/"*.sh

run env VPC_CIDR="$VPC_CIDR" ADMIN_CIDR="$ADMIN_CIDR" "$HERE/harden.sh"
log "bootstrap finished"
