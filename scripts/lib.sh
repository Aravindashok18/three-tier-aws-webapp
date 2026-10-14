#!/usr/bin/env bash
# Helpers shared by bootstrap.sh and harden.sh.
#
# DRY_RUN=1 prints what would happen instead of doing it, so the scripts can be
# read, tested and reviewed on a machine that is not an Ubuntu server.

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { printf 'error: %s\n' "$*" >&2; exit 1; }

dry_run() { [ "${DRY_RUN:-0}" = 1 ]; }

# run CMD ARGS...: execute, or print in dry-run mode.
run() {
  if dry_run; then printf 'DRY-RUN: %s\n' "$*"; else "$@"; fi
}

# render SRC DEST MODE [KEY=VALUE ...]
# Copies SRC to DEST with every __KEY__ replaced by VALUE. Replacement is done by awk's
# index/substr, not by a regex, so values containing / & or . are copied verbatim.
render() {
  local src=$1 dest=$2 mode=$3
  shift 3
  local content
  content=$(PAIRS="$(printf '%s\n' "$@")" awk '
    BEGIN { n = split(ENVIRON["PAIRS"], p, "\n") }
    {
      line = $0
      for (i = 1; i <= n; i++) {
        if (p[i] == "") continue
        eq = index(p[i], "=")
        token = "__" substr(p[i], 1, eq - 1) "__"
        value = substr(p[i], eq + 1)
        out = ""
        while ((at = index(line, token)) > 0) {
          out = out substr(line, 1, at - 1) value
          line = substr(line, at + length(token))
        }
        line = out line
      }
      print line
    }' "$src")
  if dry_run; then
    printf 'DRY-RUN: write %s (mode %s) from %s\n' "$dest" "$mode" "$(basename "$src")"
    printf '%s\n' "$content" | sed 's/^/    | /'
  else
    mkdir -p "$(dirname "$dest")"
    printf '%s\n' "$content" > "$dest"
    chmod "$mode" "$dest"
  fi
}

# write_file DEST MODE: like render, but the content comes from stdin.
write_file() {
  local dest=$1 mode=$2 content
  content=$(cat)
  if dry_run; then
    printf 'DRY-RUN: write %s (mode %s)\n' "$dest" "$mode"
    printf '%s\n' "$content" | sed 's/^/    | /'
  else
    mkdir -p "$(dirname "$dest")"
    printf '%s\n' "$content" > "$dest"
    chmod "$mode" "$dest"
  fi
}

require_root() {
  dry_run && return 0
  [ "$(id -u)" -eq 0 ] || die "run as root (sudo)"
}
