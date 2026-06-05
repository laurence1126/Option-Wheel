from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from typing import TYPE_CHECKING, Literal

import pandas as pd
from futu import OrderStatus, TrdSide

from app.utils.logging import configure_logger

if TYPE_CHECKING:
    from trading.trading_engine.futu_trading_engine import FutuTradingEngine

logger = configure_logger(__name__)


OPEN_ORDER_STATUSES = [
    OrderStatus.WAITING_SUBMIT,
    OrderStatus.SUBMITTING,
    OrderStatus.SUBMITTED,
    OrderStatus.FILLED_PART,
]

TERMINAL_ORDER_STATUSES = {
    OrderStatus.FILLED_ALL,
    OrderStatus.CANCELLED_PART,
    OrderStatus.CANCELLED_ALL,
    OrderStatus.FAILED,
    OrderStatus.DISABLED,
    OrderStatus.DELETED,
}


def round_down_to_tick(price: float, price_tick: float) -> float:
    tick = Decimal(str(price_tick))
    return float((Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_FLOOR) * tick)


def round_up_to_tick(price: float, price_tick: float) -> float:
    tick = Decimal(str(price_tick))
    return float((Decimal(str(price)) / tick).to_integral_value(rounding=ROUND_CEILING) * tick)


def build_price_ladder(
    side: Literal["buy", "sell"],
    code: str,
    bid_price: float,
    ask_price: float,
    price_tick: float,
    steps: tuple[float, ...],
) -> list[float]:
    mid_price = (bid_price + ask_price) / 2
    spread = ask_price - bid_price
    prices = []
    for step in steps:
        if side == "sell":
            price = mid_price - spread * step
            price = round_down_to_tick(price, price_tick)
            price = round(max(price, bid_price), 2)
        else:
            price = mid_price + spread * step
            price = round_up_to_tick(price, price_tick)
            price = round(min(price, ask_price), 2)
        if price not in prices:
            prices.append(price)

    logger.info("Built %s price ladder: code=%s, prices=%s", side, code, prices)
    return prices


@dataclass
class LimitOrderRequest:
    acc_id: str | int
    code: str
    side: TrdSide
    qty: int
    price: float | list[float]
    remark: str | None = None


@dataclass
class OrderWaitResult:
    order_id: str
    order_status: object | None
    dealt_qty: float
    fully_filled: bool
    terminal: bool
    timed_out: bool


@dataclass
class ExecutionResult:
    code: str
    target_qty: int
    filled_qty: float
    order_id: str | None
    order_status: object | None = None
    execution_status: str = ""
    message: str = ""


@dataclass
class FillTracker:
    target_qty: int
    total_filled_qty: float = 0.0
    order_counted_dealt_qty: float = 0.0

    @property
    def remaining_qty(self) -> int:
        return int(self.target_qty - self.total_filled_qty)

    @property
    def filled(self) -> bool:
        return self.total_filled_qty >= self.target_qty

    def reset_order(self) -> None:
        self.order_counted_dealt_qty = 0.0

    def add_wait_result(self, wait_result: OrderWaitResult) -> float:
        incremental_dealt_qty = max(0.0, wait_result.dealt_qty - self.order_counted_dealt_qty)
        self.total_filled_qty += incremental_dealt_qty
        self.order_counted_dealt_qty = max(self.order_counted_dealt_qty, wait_result.dealt_qty)
        return incremental_dealt_qty

    def add_incremental_fill(self, dealt_qty: float) -> None:
        self.total_filled_qty += dealt_qty
        self.order_counted_dealt_qty += dealt_qty


class OrderExecutionService:
    def __init__(self, engine: FutuTradingEngine, max_order_updates: int = 500) -> None:
        self.engine = engine
        self._lock = threading.RLock()
        self._order_events: dict[str, threading.Event] = {}
        self._order_updates: dict[str, pd.Series] = {}
        self._order_wait_targets: dict[str, float] = {}
        self._max_order_updates = max_order_updates

    def on_order_status(self, data: pd.DataFrame) -> None:
        for _, row in data.iterrows():
            order_id = str(row["order_id"])
            order_status = row["order_status"]
            dealt_qty = float(row["dealt_qty"]) if "dealt_qty" in row.index else 0.0

            with self._lock:
                self._order_updates[order_id] = row.copy()
                self._trim_order_updates()
                event = self._order_events.get(order_id)
                target_qty = self._order_wait_targets.get(order_id)

            if event is None:
                continue
            if order_status in TERMINAL_ORDER_STATUSES or (target_qty is not None and dealt_qty >= target_qty):
                event.set()

    def execute_limit_ladder(
        self,
        request: LimitOrderRequest,
        order_wait_seconds: int,
        cancel_wait_seconds: int,
        fill_outside_rth: bool = None,
    ) -> ExecutionResult:
        prices = request.price if isinstance(request.price, list) else [request.price]
        fill_tracker = FillTracker(target_qty=request.qty)
        last_order_id = None
        if not prices:
            logger.warning("Limit ladder execution skipped: empty price ladder for code=%s.", request.code)
            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=0.0,
                order_id=None,
                execution_status="fail",
                message="Empty price ladder.",
            )

        price_index = 0
        while price_index < len(prices):
            remaining_qty = fill_tracker.remaining_qty
            if remaining_qty <= 0:
                logger.info("Limit ladder completed: code=%s, filled_qty=%s.", request.code, fill_tracker.total_filled_qty)
                return self._filled_result(request, last_order_id, OrderStatus.FILLED_ALL, fill_tracker.total_filled_qty)

            order_id = self.engine.place_limit_order(
                acc_id=request.acc_id,
                code=request.code,
                side=request.side,
                qty=remaining_qty,
                price=prices[price_index],
                remark=request.remark,
                fill_outside_rth=fill_outside_rth,
            )
            if order_id is None:
                price_index += 1
                continue
            last_order_id = order_id
            fill_tracker.reset_order()
            submitted_qty = remaining_qty

            while price_index < len(prices):
                wait_result = self.wait_order_done_or_timeout(
                    request=request,
                    order_id=order_id,
                    submitted_qty=submitted_qty,
                    timeout_seconds=order_wait_seconds,
                )

                fill_tracker.add_wait_result(wait_result)

                if wait_result.fully_filled or fill_tracker.filled:
                    logger.info(
                        "Limit order completed: order_id=%s, code=%s, filled_qty=%s, target_qty=%s.",
                        order_id,
                        request.code,
                        fill_tracker.total_filled_qty,
                        request.qty,
                    )
                    return self._filled_result(request, order_id, wait_result.order_status, fill_tracker.total_filled_qty)

                if wait_result.terminal:
                    logger.warning(
                        "Limit order reached terminal status before full fill; continuing price ladder with a new order: order_id=%s, status=%s, dealt_qty=%s, remaining_qty=%s.",
                        order_id,
                        wait_result.order_status,
                        wait_result.dealt_qty,
                        fill_tracker.remaining_qty,
                    )
                    price_index += 1
                    break

                next_price_index = price_index + 1
                if next_price_index >= len(prices):
                    cancel_result = self.cancel_order_and_wait(
                        request=request,
                        order_id=order_id,
                        submitted_qty=submitted_qty,
                        timeout_seconds=cancel_wait_seconds,
                        previous_dealt_qty=fill_tracker.order_counted_dealt_qty,
                    )
                    final_result = self._handle_cancel_result(
                        request=request,
                        order_id=order_id,
                        last_order_id=last_order_id,
                        fill_tracker=fill_tracker,
                        cancel_result=cancel_result,
                        fallback_order_status=wait_result.order_status,
                    )
                    if final_result is not None:
                        return final_result
                    price_index = next_price_index
                    break

                next_price = prices[next_price_index]
                modified = self.engine.modify_limit_order(
                    acc_id=request.acc_id,
                    order_id=order_id,
                    qty=submitted_qty,
                    price=next_price,
                )
                if modified:
                    logger.info(
                        "Limit order modified: order_id=%s, code=%s, qty=%s, price=%.2f.",
                        order_id,
                        request.code,
                        submitted_qty,
                        next_price,
                    )
                    price_index = next_price_index
                    continue

                logger.error(
                    "Modify order failed; cancel existing order before resubmitting: order_id=%s, code=%s, next_price=%.2f.",
                    order_id,
                    request.code,
                    next_price,
                )
                cancel_result = self.cancel_order_and_wait(
                    request=request,
                    order_id=order_id,
                    submitted_qty=submitted_qty,
                    timeout_seconds=cancel_wait_seconds,
                    previous_dealt_qty=fill_tracker.order_counted_dealt_qty,
                )
                final_result = self._handle_cancel_result(
                    request=request,
                    order_id=order_id,
                    last_order_id=last_order_id,
                    fill_tracker=fill_tracker,
                    cancel_result=cancel_result,
                    fallback_order_status=wait_result.order_status,
                    context=" after modify failure",
                )
                if final_result is not None:
                    return final_result

                price_index = next_price_index
                break

            logger.info(
                "Continuing price ladder: code=%s, filled_qty=%s, remaining_qty=%s.",
                request.code,
                fill_tracker.total_filled_qty,
                fill_tracker.remaining_qty,
            )

        if fill_tracker.total_filled_qty > 0:
            logger.warning(
                "Limit price ladder exhausted with partial fill: code=%s, filled_qty=%s, target_qty=%s.",
                request.code,
                fill_tracker.total_filled_qty,
                request.qty,
            )
            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=fill_tracker.total_filled_qty,
                order_id=last_order_id,
                execution_status="fail",
                message="Price ladder exhausted after partial fill.",
            )

        logger.warning("Limit price ladder exhausted without fill: code=%s.", request.code)
        return ExecutionResult(
            code=request.code,
            target_qty=request.qty,
            filled_qty=fill_tracker.total_filled_qty,
            order_id=last_order_id,
            execution_status="fail",
            message="Price ladder exhausted without fill.",
        )

    def execute_limit_order(
        self,
        request: LimitOrderRequest,
        order_wait_seconds: int,
        cancel_wait_seconds: int,
        fill_outside_rth: bool = None,
    ) -> ExecutionResult:
        if isinstance(request.price, list):
            if not request.price:
                logger.warning("Limit order execution skipped: empty price list for code=%s.", request.code)
                return ExecutionResult(
                    code=request.code,
                    target_qty=request.qty,
                    filled_qty=0.0,
                    order_id=None,
                    execution_status="fail",
                    message="Empty price list.",
                )
            price = request.price[0]
        else:
            price = request.price

        order_id = self.engine.place_limit_order(
            acc_id=request.acc_id,
            code=request.code,
            side=request.side,
            price=price,
            qty=request.qty,
            remark=request.remark,
            fill_outside_rth=fill_outside_rth,
        )
        if order_id is None:
            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=0.0,
                order_id=None,
                execution_status="fail",
                message="Order submission failed.",
            )

        wait_result = self.wait_order_done_or_timeout(
            request=request,
            order_id=order_id,
            submitted_qty=request.qty,
            timeout_seconds=order_wait_seconds,
        )
        if wait_result.fully_filled:
            return self._filled_result(request, order_id, wait_result.order_status, wait_result.dealt_qty)

        if wait_result.timed_out:
            cancel_result = self.cancel_order_and_wait(
                request=request,
                order_id=order_id,
                submitted_qty=request.qty,
                timeout_seconds=cancel_wait_seconds,
                previous_dealt_qty=wait_result.dealt_qty,
            )
            if cancel_result is None:
                logger.error(
                    "Limit order timed out and cancel request failed; order may still be live: order_id=%s, code=%s, dealt_qty=%s, target_qty=%s.",
                    order_id,
                    request.code,
                    wait_result.dealt_qty,
                    request.qty,
                )
                return ExecutionResult(
                    code=request.code,
                    target_qty=request.qty,
                    filled_qty=wait_result.dealt_qty,
                    order_id=order_id,
                    order_status=wait_result.order_status,
                    execution_status="fail",
                    message="Order timed out and cancel request failed; order may still be live.",
                )

            filled_qty = wait_result.dealt_qty + cancel_result.dealt_qty
            if filled_qty >= request.qty:
                return self._filled_result(
                    request=request,
                    order_id=order_id,
                    order_status=cancel_result.order_status,
                    filled_qty=filled_qty,
                    message="Target quantity filled while confirming cancellation.",
                )

            if cancel_result.timed_out:
                logger.error(
                    "Limit order timed out and cancel confirmation timed out; order may still be live: order_id=%s, code=%s, filled_qty=%s, target_qty=%s.",
                    order_id,
                    request.code,
                    filled_qty,
                    request.qty,
                )
                return ExecutionResult(
                    code=request.code,
                    target_qty=request.qty,
                    filled_qty=filled_qty,
                    order_id=order_id,
                    order_status=cancel_result.order_status,
                    execution_status="fail",
                    message="Order timed out and cancel confirmation timed out; order may still be live.",
                )

            logger.warning(
                "Limit order timed out before full fill; cancel confirmed: order_id=%s, code=%s, filled_qty=%s, target_qty=%s, cancel_status=%s.",
                order_id,
                request.code,
                filled_qty,
                request.qty,
                cancel_result.order_status,
            )
            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=filled_qty,
                order_id=order_id,
                order_status=cancel_result.order_status,
                execution_status="fail",
                message="Order timed out before full fill; cancel confirmed.",
            )

        message = "Order timed out before full fill." if wait_result.timed_out else "Order ended before full fill."
        logger.warning(
            "Limit order execution incomplete: order_id=%s, code=%s, status=%s, dealt_qty=%s, target_qty=%s, timed_out=%s.",
            order_id,
            request.code,
            wait_result.order_status,
            wait_result.dealt_qty,
            request.qty,
            wait_result.timed_out,
        )
        return ExecutionResult(
            code=request.code,
            target_qty=request.qty,
            filled_qty=wait_result.dealt_qty,
            order_id=order_id,
            order_status=wait_result.order_status,
            execution_status="fail",
            message=message,
        )

    def wait_order_done_or_timeout(self, request: LimitOrderRequest, order_id: str, submitted_qty: int, timeout_seconds: int) -> OrderWaitResult:
        event = threading.Event()
        with self._lock:
            self._order_events[order_id] = event
            self._order_wait_targets[order_id] = float(submitted_qty)
            existing_update = self._order_updates.get(order_id)

        if existing_update is not None and self._is_order_wait_done(existing_update, submitted_qty):
            order_update = existing_update
        else:
            logger.info("Waiting for order push: order_id=%s, submitted_qty=%s, timeout_seconds=%s.", order_id, submitted_qty, timeout_seconds)
            event.wait(timeout_seconds)
            with self._lock:
                order_update = self._order_updates.get(order_id)

        if order_update is None or not self._is_order_wait_done(order_update, submitted_qty):
            fallback_orders = self.engine.order_list_query(acc_id=request.acc_id, order_id=order_id, code=request.code)
            if fallback_orders is not None and not fallback_orders.empty:
                order_update = fallback_orders.iloc[0]
            with self._lock:
                latest_update = self._order_updates.get(order_id)
            if latest_update is not None and self._is_order_wait_done(latest_update, submitted_qty):
                order_update = latest_update

        with self._lock:
            self._order_events.pop(order_id, None)
            self._order_wait_targets.pop(order_id, None)

        if order_update is None:
            logger.warning("Order wait timed out without order update: order_id=%s, timeout_seconds=%s.", order_id, timeout_seconds)
            return OrderWaitResult(
                order_id=order_id,
                order_status=None,
                dealt_qty=0.0,
                fully_filled=False,
                terminal=False,
                timed_out=True,
            )

        dealt_qty = float(order_update["dealt_qty"]) if "dealt_qty" in order_update.index else 0.0
        order_status = order_update["order_status"] if "order_status" in order_update.index else OrderStatus.NONE
        terminal = order_status in TERMINAL_ORDER_STATUSES
        fully_filled = order_status == OrderStatus.FILLED_ALL or dealt_qty >= submitted_qty
        timed_out = not terminal and not fully_filled
        if not timed_out:
            logger.info("Order wait completed: order_id=%s, status=%s, dealt_qty=%s.", order_id, order_status, dealt_qty)
        else:
            logger.warning(
                "Order wait timed out: order_id=%s, timeout_seconds=%s, status=%s, dealt_qty=%s, submitted_qty=%s.",
                order_id,
                timeout_seconds,
                order_status,
                dealt_qty,
                submitted_qty,
            )
        return OrderWaitResult(
            order_id=order_id,
            order_status=order_status,
            dealt_qty=dealt_qty,
            fully_filled=fully_filled,
            terminal=terminal,
            timed_out=timed_out,
        )

    def cancel_order_and_wait(
        self,
        request: LimitOrderRequest,
        order_id: str,
        submitted_qty: int,
        timeout_seconds: int,
        previous_dealt_qty: float,
    ) -> OrderWaitResult | None:
        cancel_requested = self.engine.cancel_open_orders(acc_id=request.acc_id, order_id=order_id, code=request.code)
        if not cancel_requested:
            return None

        wait_result = self.wait_order_done_or_timeout(
            request=request,
            order_id=order_id,
            submitted_qty=submitted_qty,
            timeout_seconds=timeout_seconds,
        )
        incremental_dealt_qty = max(0.0, wait_result.dealt_qty - previous_dealt_qty)
        if incremental_dealt_qty > 0:
            logger.warning(
                "Order filled during cancel confirmation: order_id=%s, previous_dealt_qty=%s, current_dealt_qty=%s, incremental_dealt_qty=%s.",
                order_id,
                previous_dealt_qty,
                wait_result.dealt_qty,
                incremental_dealt_qty,
            )
        return OrderWaitResult(
            order_id=wait_result.order_id,
            order_status=wait_result.order_status,
            dealt_qty=incremental_dealt_qty,
            fully_filled=wait_result.fully_filled,
            terminal=wait_result.terminal,
            timed_out=wait_result.timed_out,
        )

    def _handle_cancel_result(
        self,
        request: LimitOrderRequest,
        order_id: str,
        last_order_id: str | None,
        fill_tracker: FillTracker,
        cancel_result: OrderWaitResult | None,
        fallback_order_status: object | None,
        context: str = "",
    ) -> ExecutionResult | None:
        if cancel_result is None:
            reason = f"Cancel request failed{context}"
            logger.error(
                "%s; stop ladder to avoid duplicate live orders: order_id=%s, code=%s, filled_qty=%s, remaining_qty=%s.",
                reason,
                order_id,
                request.code,
                fill_tracker.total_filled_qty,
                fill_tracker.remaining_qty,
            )

            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=fill_tracker.total_filled_qty,
                order_id=last_order_id,
                order_status=fallback_order_status,
                execution_status="fail",
                message=f"{reason}.",
            )

        fill_tracker.add_incremental_fill(cancel_result.dealt_qty)
        if cancel_result.fully_filled or fill_tracker.filled:
            logger.info(
                "Limit order completed during cancel confirmation: order_id=%s, code=%s, filled_qty=%s, target_qty=%s.",
                order_id,
                request.code,
                fill_tracker.total_filled_qty,
                request.qty,
            )
            return self._filled_result(
                request=request,
                order_id=order_id,
                order_status=cancel_result.order_status,
                filled_qty=fill_tracker.total_filled_qty,
                message="Target quantity filled while confirming cancellation.",
            )

        if cancel_result.timed_out:
            reason = f"Cancel confirmation timed out{context}"
            logger.error(
                "%s; stop ladder to avoid duplicate live orders: order_id=%s, code=%s, filled_qty=%s, remaining_qty=%s.",
                reason,
                order_id,
                request.code,
                fill_tracker.total_filled_qty,
                fill_tracker.remaining_qty,
            )
            return ExecutionResult(
                code=request.code,
                target_qty=request.qty,
                filled_qty=fill_tracker.total_filled_qty,
                order_id=last_order_id,
                order_status=cancel_result.order_status,
                execution_status="fail",
                message=f"{reason}.",
            )

        return None

    def _trim_order_updates(self) -> None:
        removable_count = len(self._order_updates) - self._max_order_updates
        if removable_count <= 0:
            return

        waiting_order_ids = set(self._order_events)
        for cached_order_id in list(self._order_updates):
            if cached_order_id in waiting_order_ids:
                continue
            self._order_updates.pop(cached_order_id, None)
            removable_count -= 1
            if removable_count <= 0:
                return

    @staticmethod
    def _is_order_wait_done(order_update: pd.Series, submitted_qty: int) -> bool:
        order_status = order_update["order_status"] if "order_status" in order_update.index else None
        dealt_qty = float(order_update["dealt_qty"]) if "dealt_qty" in order_update.index else 0.0
        return order_status in TERMINAL_ORDER_STATUSES or dealt_qty >= submitted_qty

    @staticmethod
    def _filled_result(
        request: LimitOrderRequest,
        order_id: str | None,
        order_status: object | None,
        filled_qty: float,
        message: str = "Target quantity filled.",
    ) -> ExecutionResult:
        return ExecutionResult(
            code=request.code,
            target_qty=request.qty,
            filled_qty=filled_qty,
            order_id=order_id,
            order_status=order_status,
            execution_status="success",
            message=message,
        )
