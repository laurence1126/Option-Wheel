#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from trading.config import futu_config
from trading.utils import futu_utils

TIMEOUT_SECONDS = 60.0
INTERVAL_SECONDS = 1.0


def main() -> int:
    deadline = time.monotonic() + TIMEOUT_SECONDS
    last_error = "not checked yet"
    while time.monotonic() < deadline:
        ready, last_error = _check_once()
        if ready:
            return 0
        time.sleep(INTERVAL_SECONDS)

    print(
        f"Futu OpenD is not ready at {futu_config.FUTU_OPEND_ADDRESS}:{futu_config.FUTU_OPEND_PORT}: {last_error}",
        file=sys.stderr,
    )
    return 1


def _check_once() -> tuple[bool, object]:
    quote_context = None
    trade_context = None
    try:
        quote_context = futu_utils.create_quote_context(futu_config.FUTU_OPEND_ADDRESS, futu_config.FUTU_OPEND_PORT)
        ret, data = quote_context.get_global_state()
        if ret != futu_config.RET_OK:
            return False, data

        trade_context = futu_utils.create_trade_context(
            futu_config.FUTU_OPEND_ADDRESS,
            futu_config.FUTU_OPEND_PORT,
            futu_config.TRADING_MARKET,
        )
        ret, accounts = trade_context.get_acc_list()
        if ret != futu_config.RET_OK:
            return False, accounts
        if accounts.empty:
            return False, "account list is empty"

        return True, "ready"
    except Exception as exc:
        return False, exc
    finally:
        for context in (quote_context, trade_context):
            if context is None:
                continue
            try:
                context.close()
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main())
