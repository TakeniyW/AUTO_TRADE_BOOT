from __future__ import annotations

import time
from typing import Dict, List, Optional

import ccxt

from .config import EXCHANGES
from .data import ccxt_symbol, resolve_proxy


class BrokerError(RuntimeError):
    pass


class Broker:
    """ccxt 交易所适配层：统一币安 / 欧易的永续合约交易。"""

    def __init__(
        self,
        exchange_id: str,
        mode: str,
        keys: Optional[dict] = None,
        proxy: str = "",
        testnet: bool = False,
    ) -> None:
        if exchange_id not in EXCHANGES:
            raise BrokerError(f"不支持的交易所: {exchange_id}")
        self.exchange_id = exchange_id
        self.mode = mode
        self.testnet = testnet or mode == "testnet"
        self.keys = keys or {}
        opts = {
            "enableRateLimit": True,
            "timeout": 15000,
            "options": {"defaultType": "swap", "adjustForTimeDifference": True},
        }
        proxy = resolve_proxy(proxy)
        if proxy:
            opts["proxies"] = {"http": proxy, "https": proxy}
        if self.mode != "paper":
            if not (self.keys.get("api_key") and self.keys.get("secret")):
                raise BrokerError(f"{EXCHANGES[exchange_id]['name']} 未配置 API Key")
            opts["apiKey"] = self.keys["api_key"]
            opts["secret"] = self.keys["secret"]
            if self.keys.get("passphrase"):
                opts["password"] = self.keys["passphrase"]

        klass = getattr(ccxt, EXCHANGES[exchange_id]["ccxt"])
        self.ex = klass(opts)
        if self.testnet:
            self.ex.set_sandbox_mode(True)
        try:
            self.ex.load_markets()
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(self._humanize(exc)) from exc
        self._markets_loaded = True

    # ---------------- helpers ----------------
    @staticmethod
    def _humanize(exc: Exception) -> str:
        text = str(exc)
        name = type(exc).__name__
        if isinstance(exc, ccxt.AuthenticationError):
            return f"API 鉴权失败，请检查 Key / Secret / 权限：{text[:180]}"
        if isinstance(exc, ccxt.InsufficientFunds):
            return f"账户保证金不足：{text[:180]}"
        if isinstance(exc, ccxt.PermissionDenied):
            return f"API 权限不足（需开启合约交易权限）：{text[:180]}"
        if isinstance(exc, ccxt.NetworkError):
            return f"网络无法连接交易所（{name}）：{text[:180]}"
        if isinstance(exc, ccxt.RateLimitExceeded):
            return "触发交易所限频，请稍后重试"
        return f"交易所请求失败（{name}）：{text[:240]}"

    def _call(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except ccxt.BaseError as exc:
            raise BrokerError(self._humanize(exc)) from exc
        except Exception as exc:  # noqa: BLE001
            raise BrokerError(f"请求失败：{exc}") from exc

    @property
    def symbol(self) -> str:
        return self._symbol

    def set_symbol(self, ui_symbol: str) -> str:
        sym = ccxt_symbol(ui_symbol)
        if sym not in self.ex.markets:
            raise BrokerError(f"{EXCHANGES[self.exchange_id]['name']} 不支持交易对 {ui_symbol}")
        self._symbol = sym
        self.market_info = self.ex.market(sym)
        return sym

    def market(self, symbol: str) -> dict:
        return self.ex.market(ccxt_symbol(symbol))

    # ---------------- market data ----------------
    def fetch_ohlcv(self, symbol: str, timeframe: str, limit: int = 500) -> List[list]:
        sym = ccxt_symbol(symbol)
        return self._call(self.ex.fetch_ohlcv, sym, timeframe, None, limit)

    def fetch_price(self, symbol: str) -> float:
        ticker = self._call(self.ex.fetch_ticker, ccxt_symbol(symbol))
        price = ticker.get("last") or ticker.get("close")
        if not price:
            raise BrokerError("无法获取最新成交价")
        return float(price)

    # ---------------- account ----------------
    def fetch_balance(self) -> dict:
        raw = self._call(self.ex.fetch_balance)
        usdt = (raw or {}).get("USDT") or {}
        total = float(usdt.get("total") or 0)
        free = float(usdt.get("free") or 0)
        if total == 0:
            info = (raw or {}).get("info") or {}
            if isinstance(info, dict):
                try:
                    total = float(info.get("totalWalletBalance") or info.get("totalEq") or 0)
                    free = float(info.get("availableBalance") or info.get("availBal") or 0)
                except (TypeError, ValueError):
                    pass
        return {"total": total, "free": free, "raw": raw}

    def fetch_equity(self) -> float:
        bal = self.fetch_balance()
        return float(bal["total"])

    def fetch_position(self, symbol: str) -> Optional[dict]:
        sym = ccxt_symbol(symbol)
        try:
            positions = self.ex.fetch_positions([sym])
        except ccxt.BaseError as exc:
            raise BrokerError(self._humanize(exc)) from exc
        for p in positions or []:
            contracts = float(p.get("contracts") or 0)
            if contracts > 0:
                return p
        return None

    # ---------------- trading ----------------
    def set_leverage(self, symbol: str, leverage: int) -> None:
        sym = ccxt_symbol(symbol)
        try:
            self.ex.set_leverage(int(leverage), sym)
        except ccxt.BaseError as exc:
            # 已设置相同杠杆时部分交易所会报错，不阻塞开仓
            print(f"[broker] set_leverage warning: {exc}")

    def _order_params(self) -> Dict:
        if self.exchange_id == "okx":
            return {"tdMode": "cross"}
        return {}

    def open_market(self, symbol: str, side: int, notional: float) -> dict:
        """按名义价值开仓，返回实际成交信息。"""
        sym = ccxt_symbol(symbol)
        market = self.ex.market(symbol)
        price = self.fetch_price(symbol)
        contract_size = float(market.get("contractSize") or 1)
        raw_amount = notional / (price * contract_size)
        try:
            amount = float(self.ex.amount_to_precision(sym, raw_amount))
        except Exception:  # noqa: BLE001
            amount = round(raw_amount, 6)
        min_amount = float((market.get("limits") or {}).get("amount", {}).get("min") or 0)
        if min_amount and amount < min_amount:
            amount = float(self.ex.amount_to_precision(sym, min_amount))
        if amount <= 0:
            raise BrokerError("计算出的下单数量为 0，仓位过小")
        order = self._call(
            self.ex.create_order,
            sym,
            "market",
            "buy" if side > 0 else "sell",
            amount,
            None,
            self._order_params(),
        )
        return self._fill_info(order, price, contract_size)

    def close_market(self, symbol: str, position_side: int, qty: float) -> dict:
        """市价平仓。position_side: 1 多 / -1 空。"""
        sym = ccxt_symbol(symbol)
        market = self.ex.market(sym)
        contract_size = float(market.get("contractSize") or 1)
        raw_amount = qty / contract_size
        try:
            amount = float(self.ex.amount_to_precision(sym, raw_amount))
        except Exception:  # noqa: BLE001
            amount = round(raw_amount, 6)
        if amount <= 0:
            raise BrokerError("平仓数量为 0")
        params = {"reduceOnly": True}
        params.update(self._order_params())
        order = self._call(
            self.ex.create_order,
            sym,
            "market",
            "sell" if position_side > 0 else "buy",
            amount,
            None,
            params,
        )
        price = self.fetch_price(symbol)
        return self._fill_info(order, price, contract_size)

    def close_all(self, symbol: str) -> List[dict]:
        results: List[dict] = []
        try:
            positions = self.ex.fetch_positions([ccxt_symbol(symbol)])
        except ccxt.BaseError as exc:
            raise BrokerError(self._humanize(exc)) from exc
        for p in positions or []:
            contracts = float(p.get("contracts") or 0)
            if contracts <= 0:
                continue
            pside = 1 if str(p.get("side")) in ("long", "buy") else -1
            contract_size = float(p.get("contractSize") or 1)
            qty = contracts * contract_size
            results.append(self.close_market(symbol, pside, qty))
        return results

    @staticmethod
    def _fill_info(order: dict, fallback_price: float, contract_size: float) -> dict:
        price = float(order.get("average") or order.get("price") or fallback_price)
        filled_contracts = float(order.get("filled") or order.get("amount") or 0)
        qty = filled_contracts * contract_size
        fee = order.get("fee") or {}
        return {
            "id": order.get("id"),
            "price": price,
            "qty": qty,
            "notional": price * qty,
            "fee": float(fee.get("cost") or 0),
            "timestamp": order.get("timestamp") or int(time.time() * 1000),
            "status": order.get("status"),
        }

    def close(self) -> None:
        try:
            self.ex.close()
        except Exception:
            pass
