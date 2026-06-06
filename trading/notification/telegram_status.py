from __future__ import annotations

import datetime as dt
from typing import Any


def build_status_message(engine: Any) -> str:
    if engine is None:
        return "Status: 🛑 Telegram bot online, no trading engine attached."

    strategies = getattr(engine, "strategy", {})
    strategy_text = ""
    if isinstance(strategies, dict) and strategies:
        for strategy_id, strategy in strategies.items():
            strategy_text += f"    - {strategy_id}: {type(strategy).__name__}\n"
            trading_status = getattr(strategy, "trading_status", None)
            if trading_status is not None and isinstance(trading_status, dict):
                if trading_status.get("cut_loss_setup") is not None:
                    strategy_text += "        <b>·</b> Cut loss monitor " + (
                        "setup successfully.\n" if trading_status.get("cut_loss_setup") else "setup failed.\n"
                    )
                if trading_status.get("maturing_updated") is not None:
                    strategy_text += "        <b>·</b> Maturing put strikes " + (
                        "update successfully.\n" if trading_status.get("maturing_updated") else "not updated.\n"
                    )
    else:
        strategy_text = "    - None"

    duration = format_duration_since(getattr(engine, "_started_at", None))
    return (
        "<b>⚙️ Engine</b>\n"
        "    - Trading engine connected.\n"
        "    - Telegram service bot is up.\n"
        "<b>⏳ Running</b>\n"
        f"    - Status: {getattr(engine, '_running', False)}\n"
        f"    - Duration: {duration}\n"
        f"<b>💸 Strategy</b>\n"
        f"{strategy_text}"
    )


def format_duration_since(started_at: Any) -> str:
    if not isinstance(started_at, dt.datetime):
        return "N/A"

    if started_at.tzinfo is None:
        now = dt.datetime.now()
    else:
        now = dt.datetime.now(started_at.tzinfo)
    total_seconds = max(0, int((now - started_at).total_seconds()))
    hours, remainder = divmod(total_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"
