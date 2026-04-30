#!/usr/bin/env bash
set -euo pipefail

trim_line() {
  local value="${1:-}"
  value="${value//$'\r'/}"
  value="${value//$'\n'/}"
  printf '%s' "${value}"
}

if command -v scutil >/dev/null 2>&1; then
  local_host_name="$(trim_line "$(scutil --get LocalHostName 2>/dev/null || true)")"
  if [[ -n "${local_host_name}" ]]; then
    printf '%s.local\n' "${local_host_name}"
    exit 0
  fi
fi

host_name="$(trim_line "$(hostname 2>/dev/null || true)")"
case "${host_name}" in
  "")
    ;;
  localhost|localhost.localdomain)
    ;;
  *.*)
    printf '%s\n' "${host_name}"
    exit 0
    ;;
  *)
    printf '%s.local\n' "${host_name}"
    exit 0
    ;;
esac

printf '%s\n' "192.168.0.70"
