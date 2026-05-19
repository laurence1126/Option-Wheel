from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StrategyCallback:
    strategy_id: str
    action_type: str
    action_id: str | None = None


@dataclass(frozen=True)
class AssignmentCallback:
    strategy_id: str
    action_type: str
    assignment_token: str


def parse_strategy_callback(data: str) -> StrategyCallback | None:
    parts = str(data).split(":")
    if len(parts) < 3 or parts[0] != "strategy":
        return None

    _, strategy_id, action_type, *rest = parts
    if not strategy_id:
        return None

    if action_type == "retry" and len(rest) == 1 and rest[0]:
        return StrategyCallback(strategy_id=strategy_id, action_type=action_type, action_id=rest[0])
    if action_type == "cancel" and not rest:
        return StrategyCallback(strategy_id=strategy_id, action_type=action_type)
    return None


def parse_assignment_callback(data: str) -> AssignmentCallback | None:
    parts = str(data).split(":")
    if len(parts) != 4 or parts[0] != "assignment":
        return None

    _, strategy_id, action_type, assignment_token = parts
    if not strategy_id or not assignment_token:
        return None
    if action_type not in {"liquidate", "cancel", "market_order", "price_ladder"}:
        return None
    return AssignmentCallback(strategy_id=strategy_id, action_type=action_type, assignment_token=assignment_token)
