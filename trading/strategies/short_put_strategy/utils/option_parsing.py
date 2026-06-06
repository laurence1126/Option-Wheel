from __future__ import annotations

import re
import pandas as pd
from dataclasses import dataclass
from futu import TrdEnv


@dataclass
class OptionInfo:
    code: str = None
    ticker: str = None
    type: str = None
    strike: float = None
    expiration: str = None
    qty: float = None
    price: float = None


OPTION_PATTERN = {
    TrdEnv.REAL: re.compile(r"^(?P<symbol>[A-Z]+)\s+(?P<date>\d{6})\s+(?P<strike>\d+(?:\.\d+)?)(?P<type>[CP])$"),
    TrdEnv.SIMULATE: re.compile(r"^(?P<symbol>[A-Z]+)(?P<date>\d{6})(?P<type>[CP])(?P<strike>\d+)$"),
}


def resolve_option_info(option_info: OptionInfo) -> str:
    if not option_info.ticker or not option_info.expiration or option_info.strike is None or not option_info.type:
        return option_info.code

    option_type = "Put" if option_info.type == "put" else "Call"
    return f"{option_info.ticker} {option_info.strike:.2f} {option_type} ({option_info.expiration})"


def resolve_option_name(
    option_name: str, trading_environment: TrdEnv = TrdEnv.REAL, code: str = None, qty: float = None, price: float = None
) -> OptionInfo | None:
    pattern = OPTION_PATTERN.get(trading_environment, OPTION_PATTERN[TrdEnv.REAL])
    match = pattern.match(option_name)
    if not match:
        return None

    expiration = pd.to_datetime(match.group("date"), format="%y%m%d").date().isoformat()
    strike = float(match.group("strike"))
    if trading_environment == TrdEnv.SIMULATE:
        strike = strike / 1000

    return OptionInfo(
        code=code,
        ticker=match.group("symbol"),
        type="put" if match.group("type") == "P" else "call",
        strike=strike,
        expiration=expiration,
        qty=qty,
        price=price,
    )
