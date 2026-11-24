from __future__ import annotations

import threading
import time
from typing import List, Optional

from . import storage
from .broker import Broker, BrokerError
from .config import EXCHANGES, TIMEFRAMES, StrategyConfig, store
from .data import ccxt_symbol
from .indicators import ema, funding_boundaries
from .strategy import (
    base_position_now,
    desired_notional,
    leverage_to_set,
    net_pnl,
    position_multiplier,
    target_price,
)


def _now_ms() -> int:
    return int(time.time() * 1000)


class LiveEngine:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._trade_lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.broker: Optional[Broker] = None
        self.cfg: Optional[StrategyConfig] = None
        self.mode = "paper"
        self.exchange = "binance"
        self.symbol = "ETH/USDT"
        self.timeframe = "15m"
        self._candles: List[list] = []
        self._mode_key = ""
        self._last_funding_ts = 0
        self._last_equity_save = 0
        self._balance_unreal: Optional[float] = None
        self._state = self._blank_state()
        self._logs: List[dict] = []
        self.load_persisted()

    # ---------------- state ----------------
    @staticmethod
    def _blank_state() -> dict:
        return {
            "running": False,
            "ready": False,
            "mode": "paper",
            "exchange": "binance",
            "symbol": "ETH/USDT",
            "timeframe": "15m",
            "mode_key": "",
            "started_at": None,
            "last_update": None,
            "equity": 0.0,
            "cash": 0.0,
            "available": 0.0,
            "peak_equity": 0.0,
            "position": None,
            "consecutive_failures": 0,
            "position_multiplier": 1.0,
            "skip_remaining": 0,
            "base_position": 0.0,
            "target_profit_pct": 0.07,
            "last_price": None,
            "last_candle_ts": None,
            "last_action": "待启动",
            "error": None,
            "data_note": "",
            "trades_count": 0,
            "equity_history": [],
            "logs": [],
        }

    def load_persisted(self) -> None:
        saved = storage.load_live_state()
        if not saved:
            return
        saved = dict(saved)
        saved["running"] = False
        saved["ready"] = False
        saved["logs"] = []
        saved["equity_history"] = []
        saved["last_action"] = "上次会话已结束"
        self._state = saved

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._state)
            snap["logs"] = list(self._logs[-40:])
            mode_key = snap.get("mode_key") or ""
            snap["equity_history"] = storage.get_equity(mode_key, 720) if mode_key else []
            snap["position_multiplier"] = position_multiplier(
                self.cfg, snap.get("consecutive_failures", 0)
            ) if self.cfg else snap.get("position_multiplier", 1.0)
            return snap

    @property
    def running(self) -> bool:
        with self._lock:
            return bool(self._state.get("running"))

    def _log(self, message: str, level: str = "info") -> None:
        entry = {"ts": _now_ms(), "level": level, "message": message}
        self._logs.append(entry)
        if len(self._logs) > 300:
            self._logs = self._logs[-200:]
        storage.add_log(level, message, self._state.get("mode_key"))
        with self._lock:
            self._state["last_action"] = message

    def _persist(self) -> None:
        payload = dict(self._state)
        payload.pop("equity_history", None)
        payload.pop("logs", None)
        storage.save_live_state(payload)

    # ---------------- lifecycle ----------------
    def start(
        self,
        mode: str,
        exchange: str,
        symbol: str,
        strategy: StrategyConfig,
        proxy: str = "",
    ) -> dict:
        if self.running:
            raise RuntimeError("引擎已在运行中，请先停止")
        if mode not in ("paper", "testnet", "live"):
            raise RuntimeError("未知运行模式")
        if exchange not in EXCHANGES:
            raise RuntimeError("未知交易所")

        self.cfg = strategy
        self.mode = mode
        self.exchange = exchange
        self.symbol = symbol
        self.timeframe = strategy.timeframe
        self._mode_key = f"{mode}:{exchange}:{symbol}"
        self._candles = []
        self._logs = []
        self._balance_unreal = None
        self._last_funding_ts = _now_ms()
        self._last_equity_save = 0
        self._stop.clear()

        key_name = "binance_testnet" if (mode == "testnet" and exchange == "binance") else exchange
        keys = getattr(store.secrets, key_name).model_dump()
        data_note = ""
        broker: Optional[Broker] = None
        try:
            broker = Broker(
                exchange,
                mode,
                keys=keys,
                proxy=proxy,
                testnet=(mode == "testnet"),
            )
            broker.set_symbol(symbol)
            broker.fetch_ohlcv(symbol, self.timeframe, 5)
        except Exception as exc:  # noqa: BLE001
            if mode == "paper" and exchange == "binance":
                try:
                    broker = Broker(exchange, "paper", proxy=proxy, testnet=True)
                    broker.set_symbol(symbol)
                    broker.fetch_ohlcv(symbol, self.timeframe, 5)
                    data_note = "主站公共接口不可达，行情已切换至币安测试网"
                except Exception as fallback_exc:  # noqa: BLE001
                    raise BrokerError(f"{exc}；测试网回退同样失败：{fallback_exc}") from fallback_exc
            else:
                raise

        # 初始权益
        if mode == "paper":
            equity = float(strategy.initial_capital)
            available = equity
            cash = equity
        else:
            bal = broker.fetch_balance()
            equity = float(bal["total"])
            available = float(bal["free"])
            cash = available
            if equity <= 0:
                raise BrokerError("账户权益为 0，请确认已划转 USDT 至合约账户")

        with self._lock:
            self._state = self._blank_state()
            self._state.update(
                {
                    "running": True,
                    "ready": False,
                    "mode": mode,
                    "exchange": exchange,
                    "symbol": symbol,
                    "timeframe": self.timeframe,
                    "mode_key": self._mode_key,
                    "started_at": _now_ms(),
                    "equity": round(equity, 2),
                    "cash": round(cash, 2),
                    "available": round(available, 2),
                    "peak_equity": round(equity, 2),
                    "consecutive_failures": 0,
                    "skip_remaining": 0,
                    "base_position": round(base_position_now(strategy, equity), 2),
                    "target_profit_pct": strategy.target_profit_pct,
                    "last_action": "正在初始化行情与指标…",
                    "data_note": data_note,
                    "trades_count": 0,
                }
            )
        self.broker = broker
        self._persist()
        self._log(
            f"启动 {mode} · {EXCHANGES[exchange]['name']} · {symbol} · {self.timeframe}"
            + (f"（{data_note}）" if data_note else "")
        )
        self._thread = threading.Thread(target=self._run, name="liuyi-live", daemon=True)
        self._thread.start()
        return self.snapshot()

    def stop(self, close_position: bool = False) -> dict:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=15)
        if close_position:
            try:
                self._close_position("manual_stop")
            except Exception as exc:  # noqa: BLE001
                self._log(f"停止平仓失败：{exc}", "error")
        with self._lock:
            self._state["running"] = False
            self._state["ready"] = False
            self._state["last_action"] = "引擎已停止"
            self._state["last_update"] = _now_ms()
        self._persist()
        self._log("引擎已停止", "warn")
        if self.broker is not None:
            self.broker.close()
            self.broker = None
        return self.snapshot()

    def panic(self) -> dict:
        """停止并市价平掉全部仓位。"""
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive():
            thread.join(timeout=15)
        if self.broker is None and self.mode != "paper":
            keys = getattr(store.secrets, self.exchange).model_dump()
            self.broker = Broker(
                self.exchange,
                self.mode,
                keys=keys,
                proxy=store.config.proxy,
                testnet=self.mode == "testnet",
            )
            self.broker.set_symbol(self.symbol)
        if self.mode != "paper":
            try:
                self.broker.close_all(self.symbol)
                self._log("已市价平掉交易所全部仓位", "warn")
            except Exception as exc:  # noqa: BLE001
                self._log(f"全平失败：{exc}", "error")
        elif self._state.get("position"):
            if self.broker is not None:
                try:
                    self._close_position("manual_stop")
                except Exception as exc:  # noqa: BLE001
                    self._log(f"纸面平仓失败：{exc}", "error")
            else:
                self._log("纸面引擎已停止且无行情连接，直接清空本地仓位", "warn")
        with self._lock:
            self._state["position"] = None
            self._state["running"] = False
            self._state["ready"] = False
            self._state["last_action"] = "已执行一键全平"
        self._persist()
        return self.snapshot()

    # ---------------- main loop ----------------
    def _run(self) -> None:
        try:
            self._warmup()
            while not self._stop.is_set():
                try:
                    self._tick()
                except Exception as exc:  # noqa: BLE001
                    self._log(f"运行异常：{exc}", "error")
                    with self._lock:
                        self._state["error"] = str(exc)
                self._stop.wait(4.0)
        except Exception as exc:  # noqa: BLE001
            with self._lock:
                self._state["running"] = False
                self._state["ready"] = False
                self._state["error"] = str(exc)
                self._state["last_action"] = f"启动失败：{exc}"
            self._log(f"引擎启动失败：{exc}", "error")
            self._persist()

    def _closed_candles(self, candles: List[list]) -> List[list]:
        step = TIMEFRAMES[self.timeframe]
        now = _now_ms()
        return [c for c in candles if int(c[0]) + step <= now]

    def _warmup(self) -> None:
        assert self.broker is not None and self.cfg is not None
        candles = self.broker.fetch_ohlcv(self.symbol, self.timeframe, 500)
        closed = self._closed_candles(candles)
        if len(closed) < self.cfg.ema_slow + 2:
            closed = self._closed_candles(self.broker.fetch_ohlcv(self.symbol, self.timeframe, 1000))
        if len(closed) < self.cfg.ema_slow + 2:
            raise RuntimeError(f"历史 K 线不足，需要 {self.cfg.ema_slow + 2} 根，当前 {len(closed)} 根")
        self._candles = [[int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])] for c in closed][-1500:]
        last = self._candles[-1]
        price = self.broker.fetch_price(self.symbol)
        with self._lock:
            self._state["ready"] = True
            self._state["last_candle_ts"] = last[0]
            self._state["last_price"] = price
            self._state["base_position"] = round(
                base_position_now(self.cfg, self._state["peak_equity"]), 2
            )
            self._state["last_update"] = _now_ms()
        self._log(
            f"行情预热完成：{len(self._candles)} 根已收盘 K 线，最新价 {price:.2f}，等待收盘信号"
        )
        self._adopt_existing_position(price)
        self._persist()

        cross = self._cross_at_end()
        if cross:
            self._log("启动时检测到最新一根收盘 K 线出现交叉信号，按市价执行")
            self._handle_cross(cross)

    def _cross_at_end(self) -> int:
        assert self.cfg is not None
        if len(self._candles) < self.cfg.ema_slow + 2:
            return 0
        closes = [c[4] for c in self._candles]
        fast = ema(closes, self.cfg.ema_fast)
        slow = ema(closes, self.cfg.ema_slow)
        if fast[-1] is None or slow[-1] is None or fast[-2] is None or slow[-2] is None:
            return 0
        prev_d = fast[-2] - slow[-2]
        cur_d = fast[-1] - slow[-1]
        if prev_d <= 0 < cur_d:
            return 1
        if prev_d >= 0 > cur_d:
            return -1
        return 0

    def _adopt_existing_position(self, price: float) -> None:
        """实盘 / 测试网启动时接管交易所已有仓位，避免重复开仓或无法平仓。"""
        if self.mode == "paper" or self.broker is None or self.cfg is None:
            return
        info = self.broker.fetch_position(self.symbol)
        if not info:
            return
        contract_size = float(info.get("contractSize") or 1)
        qty = float(info.get("contracts") or 0) * contract_size
        entry = float(
            info.get("entryPrice")
            or info.get("entry_price")
            or (info.get("info") or {}).get("entryPrice")
            or 0
        )
        side = 1 if str(info.get("side")) in ("long", "buy") else -1
        if qty <= 0 or entry <= 0:
            return
        cfg = self.cfg
        notional = entry * qty
        entry_fee = notional * cfg.taker_fee
        target_net = cfg.target_profit_pct * notional
        tp = target_price(side, entry, qty, entry_fee, 0.0, target_net, cfg.taker_fee)
        equity = float(self._state.get("equity") or 0)
        liq = float(info.get("liquidationPrice") or 0)
        if liq <= 0:
            liq = entry - equity / qty if side > 0 else entry + equity / qty
        with self._lock:
            self._state["position"] = {
                "side": side,
                "qty": qty,
                "entry": entry,
                "notional": round(notional, 2),
                "entry_ts": _now_ms(),
                "entry_fee": entry_fee,
                "funding_paid": 0.0,
                "target_net": target_net,
                "target_px": tp,
                "leverage": round(notional / equity, 2) if equity > 0 else 0.0,
                "liquidation_px": liq,
                "unrealized": round(side * (price - entry) * qty, 2),
                "net_pnl": 0.0,
                "net_pct": 0.0,
                "adopted": True,
            }
        self._log(
            f"检测到交易所已有仓位，已接管：{'多' if side > 0 else '空'} · "
            f"{qty:.4f} · 开仓价 {entry:.4f} · 目标止盈 {tp:.4f}",
            "warn",
        )

    def _tick(self) -> None:
        assert self.broker is not None
        step = TIMEFRAMES[self.timeframe]
        recent = self._closed_candles(self.broker.fetch_ohlcv(self.symbol, self.timeframe, 6))
        if recent:
            latest = recent[-1]
            latest = [int(latest[0]), float(latest[1]), float(latest[2]), float(latest[3]), float(latest[4]), float(latest[5])]
            last_ts = self._state.get("last_candle_ts") or 0
            if latest[0] > last_ts:
                gap = latest[0] - last_ts
                # 只补入时间戳晚于已处理最后一根的 K 线，避免重复
                fresh = [
                    [int(c[0]), float(c[1]), float(c[2]), float(c[3]), float(c[4]), float(c[5])]
                    for c in recent
                    if int(c[0]) > last_ts
                ]
                if fresh:
                    self._candles.extend(fresh)
                    self._candles = self._candles[-1500:]
                with self._lock:
                    self._state["last_candle_ts"] = latest[0]
                if gap > step * 2:
                    self._log(f"检测到行情缺口（{gap // step} 根），仅处理最新收盘信号", "warn")
                cross = self._cross_at_end()
                if cross:
                    self._handle_cross(cross)
                else:
                    self._log(f"K 线收盘 {time.strftime('%H:%M', time.localtime(latest[0] / 1000))}，无交叉信号")

        price = self.broker.fetch_price(self.symbol)
        with self._lock:
            self._state["last_price"] = price
            if self._state["position"]:
                self._mark_position(price)
        self._check_target(price)
        if self.mode == "paper":
            self._accrue_funding()
        self._update_equity(price)
        self._maybe_save_equity()
        with self._lock:
            self._state["last_update"] = _now_ms()
        self._persist()

    # ---------------- signals & orders ----------------
    def _handle_cross(self, direction: int) -> None:
        with self._trade_lock:
            pos = self._state.get("position")
            if pos:
                if pos["side"] != direction:
                    self._log(
                        f"反向交叉：平{'多' if pos['side'] > 0 else '空'}并反手{'做多' if direction > 0 else '做空'}"
                    )
                    self._close_position("reverse")
                    with self._lock:
                        self._state["consecutive_failures"] += 1
                    self._open_position(direction)
            else:
                skip = int(self._state.get("skip_remaining") or 0)
                if skip > 0:
                    with self._lock:
                        self._state["skip_remaining"] = skip - 1
                    self._log(f"达标后空仓期，跳过交叉信号（剩余 {skip - 1} 次）")
                else:
                    self._open_position(direction)

    def _open_position(self, side: int) -> None:
        assert self.broker is not None and self.cfg is not None
        cfg = self.cfg
        with self._lock:
            equity = float(self._state["equity"])
            peak = float(self._state["peak_equity"])
            failures = int(self._state["consecutive_failures"])
        if equity <= 0:
            self._log("权益为 0，无法开仓", "error")
            return
        notional = desired_notional(cfg, equity, peak, failures)
        if notional < 20:
            self._log(f"可用仓位过小（{notional:.2f} USDT），跳过本次开仓", "warn")
            return
        leverage = leverage_to_set(cfg, notional, equity)
        if self.mode == "paper":
            price = self.broker.fetch_price(self.symbol)
            qty = notional / price if price > 0 else 0.0
            fill = {
                "id": None,
                "price": float(price),
                "qty": qty,
                "notional": notional,
                "fee": 0.0,
                "timestamp": _now_ms(),
                "status": "simulated",
            }
        else:
            self.broker.set_leverage(self.symbol, leverage)
            fill = self.broker.open_market(self.symbol, side, notional)
        if fill["qty"] <= 0:
            raise BrokerError("开仓成交数量为 0")
        entry_fee = fill["notional"] * cfg.taker_fee
        target_net = cfg.target_profit_pct * fill["notional"]
        tp = target_price(side, fill["price"], fill["qty"], entry_fee, 0.0, target_net, cfg.taker_fee)
        liq = (
            fill["price"] - equity / fill["qty"]
            if side > 0
            else fill["price"] + equity / fill["qty"]
        )
        with self._lock:
            if self.mode == "paper":
                self._state["cash"] = round(self._state["cash"] - entry_fee, 4)
            self._state["position"] = {
                "side": side,
                "qty": fill["qty"],
                "entry": fill["price"],
                "notional": round(fill["notional"], 2),
                "entry_ts": _now_ms(),
                "entry_fee": entry_fee,
                "funding_paid": 0.0,
                "target_net": target_net,
                "target_px": tp,
                "leverage": leverage,
                "liquidation_px": liq,
                "unrealized": 0.0,
                "net_pnl": 0.0,
                "net_pct": 0.0,
            }
            self._state["base_position"] = round(base_position_now(cfg, self._state["peak_equity"]), 2)
            self._state["skip_remaining"] = 0
        self._log(
            f"开{'多' if side > 0 else '空'} · 名义 {fill['notional']:.2f} USDT · 价格 {fill['price']:.4f} "
            f"· {leverage}x · 目标止盈 {tp:.4f}"
        )
        self._persist()

    def _close_position(self, reason: str, force_success: bool = False) -> Optional[float]:
        assert self.broker is not None and self.cfg is not None
        with self._lock:
            pos = self._state.get("position")
        if not pos:
            return None
        if self.mode == "paper":
            price = self.broker.fetch_price(self.symbol)
            qty = float(pos["qty"])
            fill = {
                "id": None,
                "price": float(price),
                "qty": qty,
                "notional": price * qty,
                "fee": 0.0,
                "timestamp": _now_ms(),
                "status": "simulated",
            }
        else:
            fill = self.broker.close_market(self.symbol, int(pos["side"]), float(pos["qty"]))
        exit_fee = fill["notional"] * self.cfg.taker_fee
        pnl, gross, _cost = net_pnl(
            int(pos["side"]),
            float(pos["entry"]),
            fill["price"],
            float(pos["qty"]),
            float(pos["entry_fee"]),
            float(pos["funding_paid"]),
            self.cfg.taker_fee,
        )
        success = force_success or pnl >= float(pos["target_net"]) - 1e-6
        if self.mode == "paper":
            with self._lock:
                self._state["cash"] = round(
                    self._state["cash"] + gross - exit_fee,
                    4,
                )
        storage.add_trade(
            self._mode_key,
            {
                "exchange": self.exchange,
                "symbol": self.symbol,
                "side": pos["side"],
                "entry_ts": pos["entry_ts"],
                "exit_ts": _now_ms(),
                "entry": pos["entry"],
                "exit": fill["price"],
                "qty": pos["qty"],
                "notional": pos["notional"],
                "pnl": pnl,
                "pnl_pct": pnl / float(pos["notional"]) * 100 if pos["notional"] else 0,
                "fees": float(pos["entry_fee"]) + exit_fee,
                "funding": pos["funding_paid"],
                "success": success,
                "reason": reason,
            },
        )
        with self._lock:
            self._state["position"] = None
            self._state["trades_count"] = int(self._state.get("trades_count") or 0) + 1
            if reason == "target" and success:
                self._state["consecutive_failures"] = 0
                self._state["skip_remaining"] = self.cfg.skip_crosses
        label = {"target": "止盈", "reverse": "反向平仓", "manual_stop": "手动平仓"}.get(reason, reason)
        self._log(
            f"{label} · 价格 {fill['price']:.4f} · 净盈亏 {pnl:+.2f} USDT（{pnl / float(pos['notional']) * 100:+.2f}%）",
            "success" if pnl > 0 else "warn",
        )
        if reason == "target" and success:
            self._log(f"单笔达标，仓位恢复基础值，空仓跳过 {self.cfg.skip_crosses} 次交叉")
        self._persist()
        return pnl

    def _check_target(self, price: float) -> None:
        with self._lock:
            pos = self._state.get("position")
        if not pos:
            return
        target_px = float(pos.get("target_px") or 0)
        if target_px <= 0:
            return
        hit = price >= target_px if int(pos["side"]) > 0 else price <= target_px
        if hit:
            with self._trade_lock:
                with self._lock:
                    if not self._state.get("position"):
                        return
                self._close_position("target", force_success=True)

    def _mark_position(self, price: float) -> None:
        pos = self._state.get("position")
        if not pos:
            return
        side = int(pos["side"])
        qty = float(pos["qty"])
        entry = float(pos["entry"])
        unreal = side * (price - entry) * qty
        net, _gross, _cost = net_pnl(
            side,
            entry,
            price,
            qty,
            float(pos["entry_fee"]),
            float(pos["funding_paid"]),
            self.cfg.taker_fee if self.cfg else 0.0005,
        )
        pos["unrealized"] = round(unreal, 2)
        pos["net_pnl"] = round(net, 2)
        pos["net_pct"] = round(net / float(pos["notional"]) * 100, 3) if pos["notional"] else 0.0

    def _accrue_funding(self) -> None:
        now = _now_ms()
        if not self.cfg:
            return
        pos = self._state.get("position")
        if not pos:
            # 空仓时不收费，但同步结算点，避免下次开仓时补扣空仓期间的资金费
            self._last_funding_ts = (now // (8 * 3600 * 1000)) * 8 * 3600 * 1000
            return
        n = funding_boundaries(self._last_funding_ts, now)
        if n <= 0:
            return
        amount = n * self.cfg.funding_fee * float(pos["notional"])
        with self._lock:
            self._state["cash"] = round(self._state["cash"] - amount, 4)
            pos = self._state.get("position")
            if pos:
                pos["funding_paid"] = round(float(pos["funding_paid"]) + amount, 4)
        self._last_funding_ts = (now // (8 * 3600 * 1000)) * 8 * 3600 * 1000
        self._log(f"模拟资金费支出 {amount:.2f} USDT（{n} 个 8h 结算点）")

    def _update_equity(self, price: float) -> None:
        if self.mode == "paper":
            pos = self._state.get("position")
            with self._lock:
                equity = float(self._state["cash"])
                if pos:
                    equity += int(pos["side"]) * (price - float(pos["entry"])) * float(pos["qty"])
                equity = max(0.0, equity)
                self._state["equity"] = round(equity, 2)
                self._state["available"] = round(equity, 2)
                self._state["peak_equity"] = round(max(float(self._state["peak_equity"]), equity), 2)
                if equity <= 0 and pos:
                    self._state["position"] = None
                    self._state["running"] = False
                    self._state["cash"] = 0.0
                    self._state["last_action"] = "纸面账户爆仓，引擎停止"
            if equity <= 0 and pos:
                storage.add_trade(
                    self._mode_key,
                    {
                        "exchange": self.exchange,
                        "symbol": self.symbol,
                        "side": pos["side"],
                        "entry_ts": pos["entry_ts"],
                        "exit_ts": _now_ms(),
                        "entry": pos["entry"],
                        "exit": price,
                        "qty": pos["qty"],
                        "notional": pos["notional"],
                        "pnl": -float(pos["notional"]),
                        "pnl_pct": -100.0,
                        "fees": float(pos["entry_fee"]),
                        "funding": pos["funding_paid"],
                        "success": False,
                        "reason": "liquidation",
                    },
                )
                self._log("纸面账户爆仓：权益归零，引擎已停止", "error")
            if equity <= 0:
                self._stop.set()
            return

        # 实盘 / 测试网：定期读取账户权益，两次读取之间用未实现盈亏增量修正
        assert self.broker is not None
        now = _now_ms()
        if now - self._last_equity_save > 25000 or self._balance_unreal is None:
            bal = self.broker.fetch_balance()
            pos = self._state.get("position")
            unreal = (
                int(pos["side"]) * (price - float(pos["entry"])) * float(pos["qty"]) if pos else 0.0
            )
            with self._lock:
                self._state["equity"] = round(float(bal["total"]), 2)
                self._state["available"] = round(float(bal["free"]), 2)
                self._state["cash"] = round(float(bal["free"]), 2)
                self._state["peak_equity"] = round(
                    max(float(self._state["peak_equity"]), float(bal["total"])), 2
                )
            self._balance_unreal = unreal
        else:
            pos = self._state.get("position")
            unreal = (
                int(pos["side"]) * (price - float(pos["entry"])) * float(pos["qty"]) if pos else 0.0
            )
            with self._lock:
                base = float(self._state["equity"]) - (self._balance_unreal or 0)
                self._state["equity"] = round(base + unreal, 2)
                self._state["peak_equity"] = round(
                    max(float(self._state["peak_equity"]), float(self._state["equity"])), 2
                )

    def _maybe_save_equity(self) -> None:
        now = _now_ms()
        if now - self._last_equity_save < 60000:
            return
        self._last_equity_save = now
        storage.add_equity(self._mode_key, now, float(self._state.get("equity") or 0))


engine = LiveEngine()
