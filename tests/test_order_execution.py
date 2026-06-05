import unittest

import pandas as pd
from futu import OrderStatus, TrdSide

from trading.trading_engine.execution_engine import LimitOrderRequest, OrderExecutionService


def order_update(order_id: str, status: object, dealt_qty: float, code: str = "US.TEST", qty: float = 10.0) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "order_id": order_id,
                "order_status": status,
                "dealt_qty": dealt_qty,
                "code": code,
                "price": 1.0,
                "trd_side": TrdSide.SELL,
                "qty": qty,
            }
        ]
    )


class FakeEngine:
    def __init__(self) -> None:
        self.service: OrderExecutionService | None = None
        self.place_results: list[str | None] = []
        self.place_updates: list[pd.DataFrame | None] = []
        self.modify_updates: list[pd.DataFrame | None] = []
        self.modify_results: list[bool] = []
        self.cancel_updates: list[pd.DataFrame | None] = []
        self.order_list_results: list[pd.DataFrame] = []
        self.cancel_result = True
        self.placed_orders: list[tuple[str, float, float]] = []
        self.modified_orders: list[tuple[str, float, float]] = []
        self.cancelled_orders: list[str] = []

    def place_limit_order(self, acc_id, code, side, qty, price, remark=None, fill_outside_rth=False):
        order_id = self.place_results.pop(0) if self.place_results else str(len(self.placed_orders) + 1)
        if order_id is None:
            return None
        self.placed_orders.append((order_id, qty, price))
        if self.place_updates:
            update = self.place_updates.pop(0)
            if update is not None:
                self.service.on_order_status(update)
        return order_id

    def modify_limit_order(self, acc_id, order_id, qty, price):
        self.modified_orders.append((order_id, qty, price))
        result = self.modify_results.pop(0) if self.modify_results else True
        if self.modify_updates:
            update = self.modify_updates.pop(0)
            if update is not None:
                self.service.on_order_status(update)
        return result

    def order_list_query(self, acc_id, order_id="", code="", status_filter_list=None, refresh_cache=True):
        if not self.order_list_results:
            return pd.DataFrame()
        return self.order_list_results.pop(0)

    def cancel_open_orders(self, acc_id, order_id="", code=""):
        self.cancelled_orders.append(order_id)
        if self.cancel_updates:
            update = self.cancel_updates.pop(0)
            if update is not None:
                self.service.on_order_status(update)
        return self.cancel_result


class OrderExecutionServiceTest(unittest.TestCase):
    def make_service(self) -> tuple[FakeEngine, OrderExecutionService, LimitOrderRequest]:
        engine = FakeEngine()
        service = OrderExecutionService(engine)
        engine.service = service
        request = LimitOrderRequest(acc_id=1, code="US.TEST", side=TrdSide.SELL, qty=10, price=[1.0], remark="test")
        return engine, service, request

    def test_cached_callback_before_wait_completes_immediately(self):
        engine, service, request = self.make_service()
        service.on_order_status(order_update("1", OrderStatus.FILLED_ALL, 10))

        result = service.wait_order_done_or_timeout(request, "1", submitted_qty=10, timeout_seconds=0)

        self.assertTrue(result.fully_filled)
        self.assertFalse(result.timed_out)
        self.assertEqual(result.dealt_qty, 10)
        self.assertEqual(engine.order_list_results, [])

    def test_timeout_uses_order_query_fallback(self):
        _, service, request = self.make_service()
        service.engine.order_list_results.append(order_update("1", OrderStatus.FILLED_ALL, 10))

        result = service.wait_order_done_or_timeout(request, "1", submitted_qty=10, timeout_seconds=0)

        self.assertTrue(result.fully_filled)
        self.assertEqual(result.order_status, OrderStatus.FILLED_ALL)
        self.assertEqual(result.dealt_qty, 10)

    def test_timeout_cancels_and_confirms_cancel_from_cached_push(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))
        engine.cancel_updates.append(order_update("1", OrderStatus.CANCELLED_ALL, 0))

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(engine.cancelled_orders, ["1"])
        self.assertEqual(result.execution_status, "fail")
        self.assertEqual(result.message, "Price ladder exhausted without fill.")
        self.assertEqual(result.filled_qty, 0)

    def test_partial_fill_during_cancel_counts_incremental_fill_only(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 2))
        engine.cancel_updates.append(order_update("1", OrderStatus.CANCELLED_PART, 5))

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "fail")
        self.assertEqual(result.message, "Price ladder exhausted after partial fill.")
        self.assertEqual(result.filled_qty, 5)

    def test_timeout_modifies_same_order_to_next_price(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))
        engine.modify_updates.append(order_update("1", OrderStatus.FILLED_ALL, 10))

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.order_id, "1")
        self.assertEqual(engine.placed_orders, [("1", 10, 1.0)])
        self.assertEqual(engine.modified_orders, [("1", 10, 0.9)])
        self.assertEqual(engine.cancelled_orders, [])

    def test_partial_fill_before_modify_is_counted_once(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 2))
        engine.modify_updates.append(order_update("1", OrderStatus.FILLED_ALL, 10))

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.filled_qty, 10)
        self.assertEqual(engine.modified_orders, [("1", 10, 0.9)])

    def test_modify_failure_cancels_then_resubmits_at_same_next_price(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))
        engine.modify_results.append(False)
        engine.cancel_updates.append(order_update("1", OrderStatus.CANCELLED_ALL, 0))
        engine.place_updates.extend([None, order_update("2", OrderStatus.FILLED_ALL, 10)])

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.order_id, "2")
        self.assertEqual(engine.modified_orders, [("1", 10, 0.9)])
        self.assertEqual(engine.cancelled_orders, ["1"])
        self.assertEqual(engine.placed_orders, [("1", 10, 1.0), ("2", 10, 0.9)])

    def test_terminal_status_before_full_fill_continues_ladder(self):
        engine, service, request = self.make_service()
        engine.place_updates.append(order_update("1", OrderStatus.FAILED, 0))
        engine.place_updates.append(order_update("2", OrderStatus.FILLED_ALL, 10))

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.order_id, "2")
        self.assertEqual(len(engine.placed_orders), 2)

    def test_retryable_cancel_status_continues_ladder(self):
        engine, service, request = self.make_service()
        engine.place_updates.append(order_update("1", OrderStatus.CANCELLED_ALL, 0))
        engine.place_updates.append(order_update("2", OrderStatus.FILLED_ALL, 10))

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.order_id, "2")
        self.assertEqual(len(engine.placed_orders), 2)

    def test_cancel_confirmation_timeout_stops_ladder(self):
        engine, service, request = self.make_service()
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "fail")
        self.assertIn("Cancel confirmation timed out", result.message)
        self.assertEqual(len(engine.placed_orders), 1)

    def test_all_order_submissions_fail_returns_fail_without_order_id(self):
        engine, service, request = self.make_service()
        engine.place_results.extend([None, None])

        request.price = [1.0, 0.9]

        result = service.execute_limit_ladder(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(result.execution_status, "fail")
        self.assertEqual(result.message, "Price ladder exhausted without fill.")
        self.assertIsNone(result.order_id)
        self.assertEqual(engine.placed_orders, [])

    def test_execute_limit_order_submits_once_and_waits_for_fill(self):
        engine, service, request = self.make_service()
        request.price = 1.0
        engine.place_updates.append(order_update("1", OrderStatus.FILLED_ALL, 10))

        result = service.execute_limit_order(request, order_wait_seconds=0, cancel_wait_seconds=0, fill_outside_rth=True)

        self.assertEqual(result.execution_status, "success")
        self.assertEqual(result.order_id, "1")
        self.assertEqual(result.filled_qty, 10)
        self.assertEqual(engine.placed_orders, [("1", 10, 1.0)])

    def test_execute_limit_order_cancels_after_timeout(self):
        engine, service, request = self.make_service()
        request.price = 1.0
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 0))
        engine.cancel_updates.append(order_update("1", OrderStatus.CANCELLED_ALL, 0))

        result = service.execute_limit_order(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(engine.cancelled_orders, ["1"])
        self.assertEqual(result.execution_status, "fail")
        self.assertEqual(result.message, "Order timed out before full fill; cancel confirmed.")
        self.assertEqual(result.order_status, OrderStatus.CANCELLED_ALL)
        self.assertEqual(result.filled_qty, 0)

    def test_execute_limit_order_counts_fill_during_cancel(self):
        engine, service, request = self.make_service()
        request.price = 1.0
        engine.order_list_results.append(order_update("1", OrderStatus.SUBMITTED, 2))
        engine.cancel_updates.append(order_update("1", OrderStatus.CANCELLED_PART, 5))

        result = service.execute_limit_order(request, order_wait_seconds=0, cancel_wait_seconds=0)

        self.assertEqual(engine.cancelled_orders, ["1"])
        self.assertEqual(result.execution_status, "fail")
        self.assertEqual(result.message, "Order timed out before full fill; cancel confirmed.")
        self.assertEqual(result.filled_qty, 5)


if __name__ == "__main__":
    unittest.main()
