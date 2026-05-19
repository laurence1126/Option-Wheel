import os
import configparser
from pathlib import Path
from typing import Literal

from futu import *
from trading.utils.logging_utils import configure_logger

logger = configure_logger(__name__)


def get_trading_pwd(config_path: str = ".config") -> str:
    env_key = os.getenv("TRADING_PWD")
    if env_key:
        return env_key

    parser = configparser.ConfigParser()
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(f"Missing {path}. Create it or set TRADING_PWD environment variable.")

    parser.read(config_path)
    try:
        trading_pwd = parser["futu_api"]["trading_pwd"].strip()
    except KeyError as exc:
        raise KeyError(f"Missing [futu_api] trading_pwd in {path}") from exc
    return trading_pwd


def get_trading_account(connection: OpenSecTradeContext, trading_env: TrdEnv, acc_type: Literal["STOCK", "OPTION"] | None = None) -> str | None:
    ret, data = connection.get_acc_list()
    if ret == RET_OK:
        result = data.loc[(data["acc_status"] == "ACTIVE") & (data["trd_env"] == trading_env)]
        if acc_type:
            result = result.loc[result["sim_acc_type"] == acc_type]
        if not result.empty:
            return result["acc_id"].iloc[0]
    else:
        logger.error("Get trading account failed: %s", data)
    return None


def create_quote_context(host: str, port: int) -> OpenQuoteContext:
    return OpenQuoteContext(host=host, port=port)


def create_trade_context(host: str, port: int, trading_market: TrdMarket) -> OpenSecTradeContext:
    return OpenSecTradeContext(
        filter_trdmarket=trading_market,
        host=host,
        port=port,
        security_firm=SecurityFirm.FUTUSECURITIES,
    )


def get_stock_account(trade_context: OpenSecTradeContext) -> str | None:
    result = get_trading_account(trade_context, TrdEnv.SIMULATE, "STOCK")
    if result:
        return result
    logger.error("No active stock account found in SIMULATE environment")


def get_option_account(trade_context: OpenSecTradeContext) -> str | None:
    result = get_trading_account(trade_context, TrdEnv.SIMULATE, "OPTION")
    if result:
        return result
    logger.error("No active option account found in SIMULATE environment")


def get_margin_account(trade_context: OpenSecTradeContext) -> str | None:
    result = get_trading_account(trade_context, TrdEnv.REAL, None)
    if result:
        return result
    logger.error("No active margin account found in REAL environment")
