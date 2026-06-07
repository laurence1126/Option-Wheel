# Ubuntu `systemd` Deployment

This deployment runs the local Python trading engine and local Futu OpenD API as `systemd` services.

## Behavior

- Starts the trading engine automatically when Ubuntu boots.
- Starts Futu OpenD automatically when Ubuntu boots.
- Restarts the engine 15 seconds after an unexpected failure.
- Restarts Futu OpenD 5 seconds after an unexpected failure.
- Waits for Futu OpenD to answer a Python SDK readiness check before starting the trading engine.
- Restarts the engine every Sunday at `03:00 America/New_York`.
- Runs strategy restart actions whenever the engine starts.
- Runs a missed weekly restart after the server comes back online.

## Prerequisites

- An Ubuntu server using `systemd`.
- The repository already checked out on the server.
- Python 3 with virtual environment support:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv
```

- The following local secret files in the repository root:

```text
.config
.RSA_private_key
```

Before installing, review `trading/config/futu_config.py`. Confirm the trading environment and remote OpenD address are correct. The installer enables and starts the engine immediately.
Confirm Futu OpenD is installed at `/home/laurence/Documents/Futu_OpenD/FutuOpenD`.

## Install

Run these commands from the repository root as your normal Ubuntu login:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -r requirements.txt
sudo deploy/install-systemd.sh "$USER" "$PWD"
```

The installer validates the virtual environment and secret files, restricts secret-file permissions, installs the service units, and enables Futu OpenD, the engine, and the weekly timer.

## Telegram Webhook

Telegram inbound commands use webhooks only. Add these fields to the repository-root `.config` file:

```ini
[telegram]
bot_token = ...
chat_id = ...
enabled = yes
webhook_base_url = https://option-wheel.ubuntu-nuc.com:8443
webhook_path_secret = use-a-long-random-path
webhook_secret_token = use-a-different-long-random-token
```

Expose `https://option-wheel.ubuntu-nuc.com:8443/<webhook_path_secret>` with a valid TLS certificate and reverse-proxy it to the local Flask app, normally `http://127.0.0.1:5001/telegram/webhook/<webhook_path_secret>`. Telegram's cloud Bot API supports webhook ports `443`, `80`, `88`, and `8443`; this deployment uses `8443` to keep the webhook separate from the watcher app.

## Verify

```bash
sudo systemctl status option-wheel.service
sudo systemctl status futu-api.service
systemctl list-timers option-wheel-weekly-restart.timer
journalctl -u option-wheel.service -n 100 --no-pager
journalctl -u futu-api.service -n 100 --no-pager
```

To follow logs continuously:

```bash
journalctl -u option-wheel.service -f
journalctl -u futu-api.service -f
```

To validate the installed units and exercise the weekly restart path manually:

```bash
sudo systemd-analyze verify /etc/systemd/system/futu-api.service /etc/systemd/system/option-wheel*.service /etc/systemd/system/option-wheel*.timer
sudo systemctl start option-wheel-weekly-restart.service
```

After a restart, confirm the logs show a graceful shutdown followed by strategy restart actions.

## Routine Operations

```bash
sudo systemctl restart option-wheel.service
sudo systemctl stop option-wheel.service
sudo systemctl start option-wheel.service
sudo systemctl status option-wheel.service
sudo systemctl restart futu-api.service
sudo systemctl stop futu-api.service
sudo systemctl start futu-api.service
sudo systemctl status futu-api.service
```

The `option-wheel.service` commands affect only the Python trading engine. The `futu-api.service` commands affect only local Futu OpenD.

## Update

After pulling new application code:

```bash
.venv/bin/python -m pip install -r requirement.txt
sudo systemctl restart option-wheel.service
```

If files under `deploy/systemd/` changed, rerun the installer:

```bash
sudo deploy/install-systemd.sh "$USER" "$PWD"
```

## Troubleshooting

Inspect recent service errors:

```bash
journalctl -u option-wheel.service -n 200 --no-pager
```

Common startup failures are missing secret files, incorrect `.config` values, an unreachable remote OpenD server, or dependencies not installed in `.venv`.
If `option-wheel.service` is waiting or failing before Python starts, inspect the Futu readiness check with `journalctl -u option-wheel.service -n 100 --no-pager`.
