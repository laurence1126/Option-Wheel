#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
    echo "Run this installer with sudo." >&2
    exit 1
fi

if [[ "$#" -ne 2 ]]; then
    echo "Usage: sudo $0 APP_USER APP_DIR" >&2
    exit 2
fi

APP_USER="$1"
APP_DIR="$(realpath "$2")"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMD_DIR="/etc/systemd/system"

if ! id "$APP_USER" >/dev/null 2>&1; then
    echo "Unknown application user: $APP_USER" >&2
    exit 1
fi

if [[ ! -x "$APP_DIR/.venv/bin/python" ]]; then
    echo "Missing virtualenv Python: $APP_DIR/.venv/bin/python" >&2
    exit 1
fi

if [[ ! -f "$APP_DIR/.config" || ! -f "$APP_DIR/.RSA_private_key" ]]; then
    echo "Missing $APP_DIR/.config or $APP_DIR/.RSA_private_key" >&2
    exit 1
fi

escape_sed_replacement() {
    printf '%s' "$1" | sed 's/[&|]/\\&/g'
}

escaped_app_user="$(escape_sed_replacement "$APP_USER")"
escaped_app_dir="$(escape_sed_replacement "$APP_DIR")"
rendered_service="$(mktemp)"
trap 'rm -f "$rendered_service"' EXIT

sed \
    -e "s|@APP_USER@|$escaped_app_user|g" \
    -e "s|@APP_DIR@|$escaped_app_dir|g" \
    "$SCRIPT_DIR/systemd/option-wheel.service" > "$rendered_service"

chmod 600 "$APP_DIR/.config" "$APP_DIR/.RSA_private_key"
install -m 0644 "$rendered_service" "$SYSTEMD_DIR/option-wheel.service"
install -m 0644 "$SCRIPT_DIR/systemd/option-wheel-weekly-restart.service" "$SYSTEMD_DIR/option-wheel-weekly-restart.service"
install -m 0644 "$SCRIPT_DIR/systemd/option-wheel-weekly-restart.timer" "$SYSTEMD_DIR/option-wheel-weekly-restart.timer"

systemctl daemon-reload
systemctl enable --now option-wheel.service
systemctl enable --now option-wheel-weekly-restart.timer
systemctl list-timers option-wheel-weekly-restart.timer
