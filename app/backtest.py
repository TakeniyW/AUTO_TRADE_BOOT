from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from .config import TIMEFRAMES, StrategyConfig
from .indicators import ema, funding_boundaries
from .strategy import (
    base_position_now,
    desired_notional,
    implied_leverage,
    net_pnl,
    position_multiplier,
    target_price,
)

MIN_NOTIONAL = 10.0


@dataclass
class Position:
    side: int
    qty: float
    entry: float
    notional: float
    entry_ts: int
    entry_index: int
    entry_fee: float
    funding_paid: float = 0.0
    target_net: float = 0.0
    margin: float = 0.0
    leverage: float = 1.0
    target_px: float = 0.0


@dataclass
class BacktestState:
    failures: int = 0
    skip: int = 0
    peak_equity: float = 0.0
    cash: float = 0.0
    fees: float = 0.0
    funding: float = 0.0
    max_leverage: float = 0.0
    max_position: float = 0.0
    cross_count: int = 0
    skipped: int = 0
    bars_in_position: int = 0
    busted: bool = False
    trades: List[dict] = field(default_factory=list)


def _trade_dict(
    pos: Position,
    exit_price: float,
    exit_ts: int,
    reason: str,
    fee_rate: float,
    equity_after: float,
    cfg: StrategyConfig,
    success: bool,
) -> dict:
    exit_fee = exit_price * pos.qty * fee_rate
    pnl, gross, _ = net_pnl(
        pos.side,
        pos.entry,
        exit_price,
        pos.qty,
        pos.entry_fee,
        pos.funding_paid,
        fee_rate,
    )
    return {
        "side": pos.side,
        "entry_ts": pos.entry_ts,
        "exit_ts": exit_ts,
        "entry": round(pos.entry, 6),
        "exit": round(exit_price, 6),
        "qty": round(pos.qty, 6),
        "notional": round(pos.notional, 2),
        "pnl": round(pnl, 2),
        "pnl_pct": round(pnl / pos.notional * 100, 3) if pos.notional else 0,
        "fees": round(pos.entry_fee + exit_fee, 2),
        "funding": round(pos.funding_paid, 2),
        "success": bool(success),
        "reason": reason,
        "leverage": round(pos.leverage, 2),
        "target_net": round(pos.target_net, 2),
        "equity_after": round(equity_after, 2),
        "bars": max(0, int((exit_ts - pos.entry_ts) / TIMEFRAMES[cfg.timeframe])),
    }


def _resample_candles(candles: Sequence[Sequence[float]], max_points: int = 1200) -> List[list]:
    n = len(candles)
    if n <= max_points:
        return [[c[0], c[1], c[2], c[3], c[4]] for c in candles]
    size = math.ceil(n / max_points)
    out: List[list] = []
    for i in range(0, n, size):
        bucket = candles[i : i + size]
        out.append(
            [
                bucket[0][0],
                bucket[0][1],
                max(b[2] for b in bucket),
                min(b[3] for b in bucket),
                bucket[-1][4],
            ]
        )
    return out


def _resample_chart(
    candles: Sequence[Sequence[float]],
    fast: Sequence[Optional[float]],
    slow: Sequence[Optional[float]],
    max_points: int = 1200,
):
    n = len(candles)
    size = 1 if n <= max_points else math.ceil(n / max_points)
    out: List[list] = []
    ef: List[Optional[float]] = []
    es: List[Optional[float]] = []
    for i in range(0, n, size):
        bucket = candles[i : i + size]
        j = i + len(bucket) - 1
        out.append(
            [
                bucket[0][0],
                bucket[0][1],
                max(b[2] for b in bucket),
                min(b[3] for b in bucket),
                bucket[-1][4],
            ]
        )
        ef.append(round(fast[j], 4) if fast[j] is not None else None)
        es.append(round(slow[j], 4) if slow[j] is not None else None)
    return out, ef, es


def _downsample_curve(curve: List[list], max_points: int = 2600) -> List[list]:
    n = len(curve)
    if n <= max_points:
        return curve
    size = math.ceil(n / (max_points // 2))
    out: List[list] = [curve[0]]
    for i in range(size, n, size):
        bucket = curve[max(0, i - size) : i]
        worst = min(bucket, key=lambda x: x[1])
        out.append(worst)
        out.append(bucket[-1])
    out.append(curve[-1])
    return out


def run_backtest(
    candles: Sequence[Sequence[float]],
    cfg: StrategyConfig,
    source: str = "binance",
) -> dict:
    if len(candles) < cfg.ema_slow + 5:
        raise ValueError(f"K 线数量不足：需要至少 {cfg.ema_slow + 5} 根，当前 {len(candles)} 根")

    closes = [float(c[4]) for c in candles]
    fast = ema(closes, cfg.ema_fast)
    slow = ema(closes, cfg.ema_slow)
    fee = cfg.taker_fee

    st = BacktestState(
        peak_equity=cfg.initial_capital,
        cash=cfg.initial_capital,
    )
    pos: Optional[Position] = None
    pending: Optional[int] = None
    curve: List[list] = []
    equity = cfg.initial_capital
    bars_per_year = 365 * 24 * 3600 * 1000 / TIMEFRAMES[cfg.timeframe]

    def close_position(exit_price: float, ts: int, reason: str, success: bool, equity_now: float) -> None:
        nonlocal pos, st
        assert pos is not None
        exit_fee = exit_price * pos.qty * fee
        gross = pos.side * (exit_price - pos.entry) * pos.qty
        st.cash += gross - exit_fee
        st.fees += exit_fee
        trade = _trade_dict(pos, exit_price, ts, reason, fee, equity_now, cfg, success)
        st.trades.append(trade)
        pos = None

    for i, (ts, o, h, l, c, _v) in enumerate(candles):
        # 1) 上一根持仓期间的资金费
        if pos is not None and i > 0:
            n = funding_boundaries(int(candles[i - 1][0]), int(ts))
            if n:
                amount = n * cfg.funding_fee * pos.notional
                st.cash -= amount
                st.funding += amount
                pos.funding_paid += amount

        # 2) 上一根收盘信号 -> 本根开盘执行
        if pending is not None:
            if pos is not None:
                equity_before = st.cash + pos.side * (o - pos.entry) * pos.qty
                close_position(o, int(ts), "reverse", False, equity_before)
                st.failures += 1
            side = pending
            pending = None
            if pos is None and st.skip > 0:
                st.skip -= 1
                st.skipped += 1
            elif pos is None and side != 0 and not st.busted:
                equity_now = max(0.0, st.cash)
                notional = desired_notional(cfg, equity_now, st.peak_equity, st.failures)
                if notional >= MIN_NOTIONAL and equity_now > 0 and o > 0:
                    qty = notional / o
                    entry_fee = notional * fee
                    st.cash -= entry_fee
                    st.fees += entry_fee
                    lev = implied_leverage(notional, equity_now)
                    pos = Position(
                        side=side,
                        qty=qty,
                        entry=o,
                        notional=notional,
                        entry_ts=int(ts),
                        entry_index=i,
                        entry_fee=entry_fee,
                        target_net=cfg.target_profit_pct * notional,
                        margin=equity_now,
                        leverage=lev,
                    )
                    st.max_leverage = max(st.max_leverage, lev)
                    st.max_position = max(st.max_position, notional)

        # 3) 盘中：爆仓优先，其次止盈
        if pos is not None:
            st.bars_in_position += 1
            tp = target_price(
                pos.side,
                pos.entry,
                pos.qty,
                pos.entry_fee,
                pos.funding_paid,
                pos.target_net,
                fee,
            )
            pos.target_px = tp
            liq_px = (
                pos.entry - pos.margin / pos.qty
                if pos.side > 0
                else pos.entry + pos.margin / pos.qty
            )
            hit_liq = l <= liq_px if pos.side > 0 else h >= liq_px
            if hit_liq:
                st.busted = True
                st.cash = 0.0
                trade = _trade_dict(pos, liq_px, int(ts), "liquidation", fee, 0.0, cfg, False)
                trade["pnl"] = -round(pos.margin, 2)
                trade["pnl_pct"] = -100.0
                st.trades.append(trade)
                pos = None
            else:
                hit_tp = h >= tp if pos.side > 0 else l <= tp
                if hit_tp:
                    equity_now = st.cash + pos.side * (tp - pos.entry) * pos.qty
                    net_now = net_pnl(
                        pos.side, pos.entry, tp, pos.qty, pos.entry_fee, pos.funding_paid, fee
                    )[0]
                    success = net_now >= pos.target_net - 1e-6
                    close_position(tp, int(ts), "target", success, equity_now)
                    if success:
                        st.failures = 0
                        st.skip = cfg.skip_crosses

        # 4) 收盘权益
        if st.busted:
            equity = 0.0
        else:
            equity = max(0.0, st.cash + (pos.side * (c - pos.entry) * pos.qty if pos else 0.0))
            if equity <= 0 and pos is not None:
                st.busted = True
                st.cash = 0.0
                trade = _trade_dict(pos, float(c), int(ts), "liquidation", fee, 0.0, cfg, False)
                trade["pnl"] = -round(pos.margin, 2)
                trade["pnl_pct"] = -100.0
                st.trades.append(trade)
                pos = None
                equity = 0.0
        st.peak_equity = max(st.peak_equity, equity)
        dd = (st.peak_equity - equity) / st.peak_equity * 100 if st.peak_equity > 0 else 0.0
        curve.append([int(ts), round(equity, 4), round(dd, 4)])

        if st.busted:
            continue

        # 5) 收盘检测交叉
        cross = 0
        if (
            i > 0
            and fast[i] is not None
            and slow[i] is not None
            and fast[i - 1] is not None
            and slow[i - 1] is not None
        ):
            prev_d = fast[i - 1] - slow[i - 1]
            cur_d = fast[i] - slow[i]
            if prev_d <= 0 < cur_d:
                cross = 1
            elif prev_d >= 0 > cur_d:
                cross = -1
        if cross:
            st.cross_count += 1
            if pos is not None:
                if pos.side != cross:
                    pending = cross
            else:
                if st.skip > 0:
                    st.skip -= 1
                    st.skipped += 1
                else:
                    pending = cross

    # 6) 期末平仓
    if pos is not None:
        last_ts = int(candles[-1][0])
        last_price = float(candles[-1][4])
        equity_before = st.cash + pos.side * (last_price - pos.entry) * pos.qty
        net_now = net_pnl(
            pos.side, pos.entry, last_price, pos.qty, pos.entry_fee, pos.funding_paid, fee
        )[0]
        success = net_now >= pos.target_net - 1e-6
        close_position(last_price, last_ts, "end", success, equity_before)
        equity = max(0.0, st.cash)
        if curve:
            curve[-1][1] = round(equity, 4)
            peak = st.peak_equity or equity
            curve[-1][2] = round((peak - equity) / peak * 100, 4) if peak else 0.0

    final_equity = round(equity, 2)
    metric_trades = st.trades
    wins = [t for t in metric_trades if t["pnl"] > 0]
    losses = [t for t in metric_trades if t["pnl"] <= 0]
    gross_win = sum(t["pnl"] for t in wins)
    gross_loss = abs(sum(t["pnl"] for t in losses))
    max_dd = max((p[2] for p in curve), default=0.0)

    returns = []
    for i in range(1, len(curve)):
        prev = curve[i - 1][1]
        if prev > 0:
            returns.append(curve[i][1] / prev - 1)
    sharpe = None
    if len(returns) > 2:
        mean = sum(returns) / len(returns)
        var = sum((r - mean) ** 2 for r in returns) / len(returns)
        std = math.sqrt(var)
        if std > 0:
            sharpe = mean / std * math.sqrt(bars_per_year)

    total_fees = round(st.fees, 2)
    total_funding = round(st.funding, 2)
    metrics = {
        "initial_capital": cfg.initial_capital,
        "final_equity": final_equity,
        "net_profit": round(final_equity - cfg.initial_capital, 2),
        "total_return_pct": round((final_equity / cfg.initial_capital - 1) * 100, 2),
        "max_drawdown_pct": round(max_dd, 2),
        "trades": len(metric_trades),
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / len(metric_trades) * 100, 1) if metric_trades else 0.0,
        "avg_pnl": round(sum(t["pnl"] for t in metric_trades) / len(metric_trades), 2)
        if metric_trades
        else 0.0,
        "avg_win": round(gross_win / len(wins), 2) if wins else 0.0,
        "avg_loss": round(-gross_loss / len(losses), 2) if losses else 0.0,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_fees": total_fees,
        "total_funding": total_funding,
        "max_leverage_used": round(st.max_leverage, 2),
        "avg_leverage": round(
            sum(t["leverage"] for t in metric_trades) / len(metric_trades), 2
        )
        if metric_trades
        else 0.0,
        "max_position": round(st.max_position, 2),
        "sharpe": round(sharpe, 2) if sharpe is not None else None,
        "crosses": st.cross_count,
        "skipped_crosses": st.skipped,
        "liquidation": bool(st.busted),
        "exposure_pct": round(st.bars_in_position / len(candles) * 100, 1),
        "avg_hold_hours": round(
            sum(t["bars"] for t in metric_trades) / len(metric_trades) * TIMEFRAMES[cfg.timeframe] / 3600000,
            1,
        )
        if metric_trades
        else 0.0,
        "start_ts": int(candles[0][0]),
        "end_ts": int(candles[-1][0]),
        "bars": len(candles),
        "final_base_position": round(base_position_now(cfg, st.peak_equity), 2),
        "peak_equity": round(st.peak_equity, 2),
        "base_position_now": round(base_position_now(cfg, st.peak_equity), 2),
        "position_multiplier_now": round(position_multiplier(cfg, st.failures), 2),
    }

    chart_candles, chart_fast, chart_slow = _resample_chart(candles, fast, slow)

    return {
        "metrics": metrics,
        "equity_curve": _downsample_curve(curve),
        "candles": chart_candles,
        "ema_fast": chart_fast,
        "ema_slow": chart_slow,
        "trades": st.trades,
        "config": cfg.model_dump(),
        "source": source,
    }
