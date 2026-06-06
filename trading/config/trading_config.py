from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ShortPutLiveConfig:
    underlying: str = "US.SPY"
    max_capital: float = 100_000.00
    target_delta: float = 0.2
    stop_loss_multiple: float | None = 1.5
    take_profit_multiple: float | None = None
    target_exp_days: int = 12
    leverage_ratio: float = 5.0
    expiration_direction: Literal["closest", "larger", "smaller"] = "larger"
    min_credit: float = 0.1
    min_volume: int = 20
    max_spread_pct: float = 0.1
    max_contracts_per_trade: int | None = 20
    max_order_book_participation: float = 0.25
    price_ladder_steps: tuple[float, ...] = (0.0, 0.25, 0.5, 0.75, 1.0)
    order_wait_seconds: int = 5
    cancel_wait_seconds: int = 5
    telegram_approval: dict[str, bool] = field(default_factory=lambda: {"short_put": False, "cut_loss": False})
    telegram_approval_timeout: int = 60


SHORT_PUT_CONFIG = ShortPutLiveConfig(
    underlying="US.SPY",
    max_capital=1_000_000,
    target_delta=0.2,
    stop_loss_multiple=1.5,
    take_profit_multiple=None,
    target_exp_days=12,
    leverage_ratio=5.0,
)
