#!/usr/bin/env bash
# Cron health check: restarts Nginx or the app when it is down or not answering.
#
# Installed by bootstrap.sh to run every 5 minutes. For each service it checks that
# systemd says the unit is active AND that it answers an HTTP request, because a
# process can be "active" and still be wedged. Exit status is 1 if anything is still
# unhealthy after a restart, so a monitor watching the log or cron mail can see it.
#
# A service that has already been restarted MAX_RESTARTS_PER_HOUR times in the last
# hour is left alone: restarting forever hides a real fault and can make it worse.
#
# Settings (all optional, mostly here so tests can point the script at stubs):
#   APP_URL, NGINX_URL, MAX_RESTARTS_PER_HOUR, SETTLE_SECONDS, CURL_TIMEOUT, STATE_DIR
set -u

APP_URL="${APP_URL:-http://127.0.0.1:8000/health}"
NGINX_URL="${NGINX_URL:-http://127.0.0.1/nginx-health}"
MAX_RESTARTS_PER_HOUR="${MAX_RESTARTS_PER_HOUR:-3}"
SETTLE_SECONDS="${SETTLE_SECONDS:-5}"
CURL_TIMEOUT="${CURL_TIMEOUT:-5}"
STATE_DIR="${STATE_DIR:-/var/lib/webapp-health}"
FAILED=0

say() {
  local line
  line="$(date '+%Y-%m-%d %H:%M:%S') $*"
  echo "$line"
  command -v logger >/dev/null 2>&1 && logger -t webapp-health -- "$*"
  return 0
}

healthy() {  # healthy SERVICE URL
  systemctl is-active --quiet "$1" && curl -fsS --max-time "$CURL_TIMEOUT" -o /dev/null "$2" 2>/dev/null
}

restarts_in_last_hour() {  # prunes entries older than an hour, prints how many remain
  local file="$STATE_DIR/$1.restarts" cutoff keep
  cutoff=$(( $(date +%s) - 3600 ))
  [ -f "$file" ] || { echo 0; return; }
  keep=$(awk -v cutoff="$cutoff" '$1 >= cutoff' "$file")
  if [ -n "$keep" ]; then printf '%s\n' "$keep" > "$file"; echo "$keep" | wc -l | tr -d ' '; else : > "$file"; echo 0; fi
}

check() {  # check SERVICE URL
  local svc=$1 url=$2 recent
  healthy "$svc" "$url" && return 0

  say "UNHEALTHY $svc (unit inactive or $url not answering)"
  recent=$(restarts_in_last_hour "$svc")
  if [ "$recent" -ge "$MAX_RESTARTS_PER_HOUR" ]; then
    say "GIVING UP on $svc: already restarted $recent times in the last hour, needs a human"
    FAILED=1
    return 0
  fi

  date +%s >> "$STATE_DIR/$svc.restarts"
  if ! systemctl restart "$svc"; then
    say "FAILED to restart $svc"
    FAILED=1
    return 0
  fi
  sleep "$SETTLE_SECONDS"
  if healthy "$svc" "$url"; then
    say "RECOVERED $svc after restart"
  else
    say "STILL UNHEALTHY $svc after restart"
    FAILED=1
  fi
}

mkdir -p "$STATE_DIR" || { echo "cannot create $STATE_DIR" >&2; exit 2; }

# One run at a time. mkdir is atomic everywhere, unlike flock which macOS does not ship.
LOCK="$STATE_DIR/lock"
if ! mkdir "$LOCK" 2>/dev/null; then
  holder=$(cat "$LOCK/pid" 2>/dev/null || echo "")
  if [ -n "$holder" ] && kill -0 "$holder" 2>/dev/null; then
    say "previous check (pid $holder) is still running, skipping"
    exit 0
  fi
  rm -rf "$LOCK"
  mkdir "$LOCK" 2>/dev/null || exit 0
fi
echo $$ > "$LOCK/pid"
trap 'rm -rf "$LOCK"' EXIT

# Backend first: if Nginx is up but the app is down, restarting Nginx would not help.
check webapp "$APP_URL"
check nginx "$NGINX_URL"

[ "$FAILED" -eq 0 ] && exit 0 || exit 1
