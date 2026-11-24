from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Dict, List

from pydantic import BaseModel, Field, field_validator

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
CACHE_DIR = DATA_DIR / "cache"
WEB_DIR = ROOT / "web"
DB_PATH = DATA_DIR / "quant.db"
CONFIG_PATH = DATA_DIR / "config.json"
SECRETS_PATH = DATA_DIR / "secrets.json"
PRESETS_PATH = DATA_DIR / "presets.json"

DATA_DIR.mkdir(parents=True, exist_ok=True)
CACHE_DIR.mkdir(parents=True, exist_ok=True)

TIMEFRAMES = {
    "1m": 60_000,
    "5m": 300_000,
    "15m": 900_000,
    "30m": 1_800_000,
    "1h": 3_600_000,
    "4h": 14_400_000,
    "1d": 86_400_000,
}

EXCHANGES = {
    "binance": {"name": "币安 Binance", "ccxt": "binanceusdm", "password": False},
    "okx": {"name": "欧易 OKX", "ccxt": "okx", "password": True},
}

# 常用永续合约（UI 下拉框；可从交易所动态补充）
POPULAR_SYMBOLS = [
    "ETH/USDT",
    "BTC/USDT",
    "SOL/USDT",
    "BNB/USDT",
    "XRP/USDT",
    "DOGE/USDT",
    "ADA/USDT",
    "LINK/USDT",
    "AVAX/USDT",
    "SUI/USDT",
    "ARB/USDT",
    "OP/USDT",
    "TON/USDT",
    "LTC/USDT",
    "BCH/USDT",
    "DOT/USDT",
    "TRX/USDT",
    "NEAR/USDT",
    "APT/USDT",
    "INJ/USDT",
]


class StrategyConfig(BaseModel):
    """刘一策略参数。仓位以 USDT 名义价值（notional）计。"""

    name: str = "刘一策略"
    symbol: str = "ETH/USDT"
    timeframe: str = "15m"

    ema_fast: int = Field(25, ge=2, le=500)
    ema_slow: int = Field(99, ge=3, le=1000)

    initial_capital: float = Field(10_000, gt=0)
    base_position: float = Field(10_000, gt=0)

    target_profit_pct: float = Field(0.07, gt=0, le=1, description="单笔净收益目标 / 仓位")

    add_after_failures: int = Field(4, ge=1, le=100)
    add_position_pct: float = Field(0.5, ge=0, le=5)
    max_position_multiplier: float = Field(5.0, ge=1, le=50)

    skip_crosses: int = Field(20, ge=0, le=500)
    max_leverage: float = Field(30, ge=1, le=125)

    growth_per_1000: float = Field(500, ge=0, description="峰值权益每 +1000 增加的基础仓位")

    taker_fee: float = Field(0.0005, ge=0, le=0.01, description="单边 taker 费率")
    funding_fee: float = Field(0.0001, ge=0, le=0.01, description="每 8 小时资金费率")

    leverage_mode: str = Field("auto", description="auto=按仓位/权益推算并钳制, fixed=固定杠杆")
    fixed_leverage: float = Field(1.0, ge=1, le=125)

    @field_validator("ema_slow")
    @classmethod
    def _slow_gt_fast(cls, v: int, info):
        fast = info.data.get("ema_fast", 0)
        if fast and v <= fast:
            raise ValueError("慢线周期必须大于快线周期")
        return v

    @field_validator("timeframe")
    @classmethod
    def _tf_ok(cls, v: str):
        if v not in TIMEFRAMES:
            raise ValueError(f"不支持的周期 {v}")
        return v


class AppConfig(BaseModel):
    strategy: StrategyConfig = Field(default_factory=StrategyConfig)
    exchange: str = "binance"
    data_source: str = "binance"
    proxy: str = ""
    default_mode: str = "paper"  # paper / testnet / live
    risk_ack: bool = False


class ExchangeKey(BaseModel):
    api_key: str = ""
    secret: str = ""
    passphrase: str = ""

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.secret)


class Secrets(BaseModel):
    binance: ExchangeKey = Field(default_factory=ExchangeKey)
    binance_testnet: ExchangeKey = Field(default_factory=ExchangeKey)
    okx: ExchangeKey = Field(default_factory=ExchangeKey)


class Store:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self.config = self._load_config()
        self.secrets = self._load_secrets()
        self.presets = self._load_presets()

    # ---------- config ----------
    def _load_config(self) -> AppConfig:
        try:
            if CONFIG_PATH.exists():
                return AppConfig.model_validate_json(CONFIG_PATH.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover - 容错
            print(f"[config] load failed: {exc}")
        cfg = AppConfig()
        self._write_json(CONFIG_PATH, cfg.model_dump())
        return cfg

    def save_config(self, cfg: AppConfig) -> AppConfig:
        with self._lock:
            self.config = cfg
            self._write_json(CONFIG_PATH, cfg.model_dump())
            return self.config

    def update_strategy(self, data: dict) -> StrategyConfig:
        with self._lock:
            merged = self.config.strategy.model_dump()
            merged.update({k: v for k, v in data.items() if k in merged})
            strategy = StrategyConfig(**merged)
            cfg = self.config.model_copy(update={"strategy": strategy})
            return self.save_config(cfg).strategy

    # ---------- secrets ----------
    def _load_secrets(self) -> Secrets:
        try:
            if SECRETS_PATH.exists():
                return Secrets.model_validate_json(SECRETS_PATH.read_text("utf-8"))
        except Exception as exc:  # pragma: no cover
            print(f"[secrets] load failed: {exc}")
        return Secrets()

    def save_secrets(self, secrets: Secrets) -> None:
        with self._lock:
            self.secrets = secrets
            self._write_json(SECRETS_PATH, secrets.model_dump(), private=True)

    def masked_secrets(self) -> Dict[str, dict]:
        def mask(value: str) -> str:
            if not value:
                return ""
            if len(value) <= 8:
                return "*" * len(value)
            return f"{value[:4]}{'*' * 8}{value[-4:]}"

        out: Dict[str, dict] = {}
        for name in ("binance", "binance_testnet", "okx"):
            key: ExchangeKey = getattr(self.secrets, name)
            out[name] = {
                "api_key": mask(key.api_key),
                "secret": mask(key.secret),
                "passphrase": mask(key.passphrase),
                "configured": key.configured,
            }
        return out

    # ---------- presets ----------
    def _load_presets(self) -> List[dict]:
        try:
            if PRESETS_PATH.exists():
                return json.loads(PRESETS_PATH.read_text("utf-8"))
        except Exception:
            pass
        return [StrategyConfig().model_dump()]

    def save_preset(self, strategy: StrategyConfig) -> List[dict]:
        with self._lock:
            presets = [p for p in self.presets if p.get("name") != strategy.name]
            presets.insert(0, strategy.model_dump())
            self.presets = presets
            self._write_json(PRESETS_PATH, presets)
            return presets

    def delete_preset(self, name: str) -> List[dict]:
        with self._lock:
            self.presets = [p for p in self.presets if p.get("name") != name]
            self._write_json(PRESETS_PATH, self.presets)
            return self.presets

    # ---------- io ----------
    @staticmethod
    def _write_json(path: Path, payload, private: bool = False) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
        os.replace(tmp, path)
        if private:
            try:
                os.chmod(path, 0o600)
            except OSError:
                pass


store = Store()
