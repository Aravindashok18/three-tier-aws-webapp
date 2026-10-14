#!/usr/bin/env bash
# Locks down an Ubuntu app server: key-only SSH, a default-deny firewall, fail2ban
# and automatic security updates. Safe to run more than once.
#
#   VPC_CIDR    required; the load balancer's health checks come from inside it
#   ADMIN_CIDR  where SSH may come from; empty means SSH stays closed (use SSM Session Manager)
#   DRY_RUN=1   print the commands and files instead of applying them
#
# Afterwards, scripts/audit-ports.sh lists what is still listening on the network.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG="$HERE/../config"
# shellcheck source=lib.sh
. "$HERE/lib.sh"

: "${VPC_CIDR:?set VPC_CIDR, for example 10.0.0.0/16}"
ADMIN_CIDR="${ADMIN_CIDR:-}"
SSH_USER="${SSH_USER:-ubuntu}"
AUTH_KEYS="${AUTH_KEYS_FILE:-/home/$SSH_USER/.ssh/authorized_keys}"
SSHD_DROPIN=/etc/ssh/sshd_config.d/00-webapp-hardening.conf

require_root

# --- SSH: keys only -----------------------------------------------------------------
if [ ! -s "$AUTH_KEYS" ]; then
  log "warning: $AUTH_KEYS is missing or empty. Nobody can log in over SSH; use Session Manager."
fi
render "$CONFIG/sshd-hardening.conf" "$SSHD_DROPIN" 644
if ! dry_run; then
  # Never reload a config sshd rejects: that is how people lock themselves out.
  if ! sshd -t; then
    rm -f "$SSHD_DROPIN"
    die "sshd rejected the hardened config; removed it and left SSH unchanged"
  fi
fi
run systemctl reload ssh

# --- firewall: deny by default ------------------------------------------------------
# The security group already filters at the VPC edge; this is a second layer on the host itself.
run ufw default deny incoming
run ufw default allow outgoing
if [ -n "$ADMIN_CIDR" ]; then
  run ufw allow from "$ADMIN_CIDR" to any port 22 proto tcp comment 'ssh from the admin address'
fi
run ufw allow from "$VPC_CIDR" to any port 80 proto tcp comment 'http from the load balancer'
run ufw --force enable

# --- fail2ban: ban hosts that keep failing SSH logins -------------------------------
render "$CONFIG/fail2ban-jail.local" /etc/fail2ban/jail.d/00-webapp.local 644 "VPC_CIDR=$VPC_CIDR"
run systemctl enable fail2ban
run systemctl restart fail2ban

# --- automatic security updates -----------------------------------------------------
render "$CONFIG/auto-upgrades" /etc/apt/apt.conf.d/20auto-upgrades 644

log "hardening applied"
run "$HERE/audit-ports.sh"
