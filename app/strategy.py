from __future__ import annotations

from typing import Optional, Tuple

from .config import StrategyConfig


def base_position_now(cfg: StrategyConfig, peak_equity: float) -> float:
    """峰值权益每增加 1000 USDT，基础仓位增加 growth_per_1000。"""
    gain = max(0.0, peak_equity - cfg.initial_capital)
    steps = int(gain // 1000)
    return cfg.base_position + steps * cfg.growth_per_1000


def position_multiplier(cfg: StrategyConfig, consecutive_failures: int) -> float:
    """连续未达标 N 笔后，仓位增加基础仓位的 add_position_pct。"""
    adds = int(consecutive_failures // cfg.add_after_failures) * cfg.add_position_pct
    return min(1.0 + adds, cfg.max_position_multiplier)


def equity_leverage_cap(cfg: StrategyConfig, equity: float) -> float:
    return max(0.0, equity) * cfg.max_leverage


def desired_notional(
    cfg: StrategyConfig,
    equity: float,
    peak_equity: float,
    consecutive_failures: int,
) -> float:
    base = base_position_now(cfg, peak_equity)
    mult = position_multiplier(cfg, consecutive_failures)
    if cfg.leverage_mode == "fixed":
        raw = base * mult * cfg.fixed_leverage
    else:
        raw = base * mult
    return min(raw, equity_leverage_cap(cfg, equity))


def target_price(
    side: int,
    entry: float,
    qty: float,
    entry_fee: float,
    funding_paid: float,
    net_target: float,
    fee_rate: float,
) -> float:
    """在给定净收益目标下，反解止盈触发价（已计入双边手续费与资金费）。"""
    if qty <= 0:
        return entry
    if side > 0:
        return (net_target + entry_fee + funding_paid + entry * qty) / (qty * (1 - fee_rate))
    return (entry * qty - net_target - entry_fee - funding_paid) / (qty * (1 + fee_rate))


def net_pnl(
    side: int,
    entry: float,
    price: float,
    qty: float,
    entry_fee: float,
    funding_paid: float,
    fee_rate: float,
) -> Tuple[float, float, float]:
    """返回 (净收益, 毛收益, 手续费+资金费)。"""
    gross = side * (price - entry) * qty
    exit_fee = price * qty * fee_rate
    costs = entry_fee + exit_fee + funding_paid
    return gross - costs, gross, costs


def implied_leverage(notional: float, equity: float) -> float:
    if equity <= 0:
        return 0.0
    return notional / equity


def leverage_to_set(cfg: StrategyConfig, notional: float, equity: float) -> int:
    """交易所实际设置的杠杆倍数（向上取整，钳制在 1..max）。"""
    lev = implied_leverage(notional, equity)
    lev = min(max(lev, 1.0), cfg.max_leverage)
    return int(min(int(lev) + (0 if lev == int(lev) else 1), int(cfg.max_leverage)))


def next_target_px_state(
    cfg: StrategyConfig,
    side: int,
    entry: float,
    qty: float,
    entry_fee: float,
    funding_paid: float,
    notional: float,
) -> Optional[float]:
    target = target_price(
        side,
        entry,
        qty,
        entry_fee,
        funding_paid,
        cfg.target_profit_pct * notional,
        cfg.taker_fee,
    )
    return target if target > 0 else None
