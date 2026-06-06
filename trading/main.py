import sys
import signal
import threading
from pathlib import Path

if not __package__:
    sys.path.append(str(Path(__file__).resolve().parents[1]))

from futu import *
from trading.config.trading_config import SHORT_PUT_CONFIG
from trading.trading_engine.futu_trading_engine import FutuTradingEngine
from trading.strategies.short_put_strategy.strategy_main import ShortPutStrategy
from app.utils.logging import configure_logger

logger = configure_logger(__name__)


def main() -> None:
    strategy = ShortPutStrategy(SHORT_PUT_CONFIG)
    engine = FutuTradingEngine(strategy)
    engine.run()

    stop_event = threading.Event()

    def request_stop(signum, _) -> None:
        logger.info("Received signal %s. Stopping trading engine.", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    try:
        stop_event.wait()
    finally:
        engine.close()


if __name__ == "__main__":
    main()
