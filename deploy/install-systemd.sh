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
futu_config_values="$(
    cd "$APP_DIR"
    "$APP_DIR/.venv/bin/python" - <<'PY'
import ast
from pathlib import Path

config_path = Path("trading/config/futu_config.py")
values = {}
for node in ast.parse(config_path.read_text()).body:
    if not isinstance(node, ast.Assign):
        continue
    for target in node.targets:
        if isinstance(target, ast.Name) and target.id in {"FUTU_OPEND_ADDRESS", "FUTU_OPEND_PORT"}:
            values[target.id] = ast.literal_eval(node.value)

print(values["FUTU_OPEND_ADDRESS"])
print(values["FUTU_OPEND_PORT"])
PY
)"
futu_opend_address="$(printf '%s\n' "$futu_config_values" | sed -n '1p')"
futu_opend_port="$(printf '%s\n' "$futu_config_values" | sed -n '2p')"
escaped_futu_opend_address="$(escape_sed_replacement "$futu_opend_address")"
escaped_futu_opend_port="$(escape_sed_replacement "$futu_opend_port")"
rendered_service="$(mktemp)"
rendered_futu_api_service="$(mktemp)"
trap 'rm -f "$rendered_service" "$rendered_futu_api_service"' EXIT

sed \
    -e "s|@APP_USER@|$escaped_app_user|g" \
    -e "s|@APP_DIR@|$escaped_app_dir|g" \
    -e "s|@FUTU_OPEND_ADDRESS@|$escaped_futu_opend_address|g" \
    -e "s|@FUTU_OPEND_PORT@|$escaped_futu_opend_port|g" \
    "$SCRIPT_DIR/systemd/option-wheel.service" > "$rendered_service"

sed \
    -e "s|@APP_USER@|$escaped_app_user|g" \
    "$SCRIPT_DIR/systemd/futu-api.service" > "$rendered_futu_api_service"

chmod 600 "$APP_DIR/.config" "$APP_DIR/.RSA_private_key"
install -m 0644 "$rendered_futu_api_service" "$SYSTEMD_DIR/futu-api.service"
install -m 0644 "$rendered_service" "$SYSTEMD_DIR/option-wheel.service"
install -m 0644 "$SCRIPT_DIR/systemd/option-wheel-weekly-restart.service" "$SYSTEMD_DIR/option-wheel-weekly-restart.service"
install -m 0644 "$SCRIPT_DIR/systemd/option-wheel-weekly-restart.timer" "$SYSTEMD_DIR/option-wheel-weekly-restart.timer"

systemctl daemon-reload
systemctl enable --now futu-api.service
systemctl enable --now option-wheel.service
systemctl enable --now option-wheel-weekly-restart.timer
systemctl list-timers option-wheel-weekly-restart.timer
