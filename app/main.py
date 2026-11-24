from __future__ import annotations

import asyncio
import json
import math
import threading
import time
import traceback
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, storage
from .backtest import run_backtest
from .config import (
    EXCHANGES,
    POPULAR_SYMBOLS,
    TIMEFRAMES,
    AppConfig,
    ExchangeKey,
    Secrets,
    StrategyConfig,
    WEB_DIR,
    store,
)
from .data import DataError, probe, sync_binance_archive, sync_okx
from .live import engine

app = FastAPI(title="刘一策略 · 合约交易终端", version=__version__)

# ---------------- jobs ----------------
JOBS: Dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
LAST_BACKTEST: Optional[dict] = None


def _job_update(job_id: str, **fields) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(fields)


def start_job(kind: str, fn: Callable[[Callable], Any]) -> str:
    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "id": job_id,
            "kind": kind,
            "status": "running",
            "progress": 0.0,
            "message": "任务已创建",
            "result": None,
            "error": None,
            "created_at": int(time.time() * 1000),
        }

    def progress(pct: float, message: str) -> None:
        _job_update(job_id, progress=round(min(99.5, max(0.0, pct)), 1), message=message)

    def runner() -> None:
        try:
            result = fn(progress)
            _job_update(job_id, status="done", progress=100.0, message="完成", result=result)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            _job_update(job_id, status="error", message="任务失败", error=str(exc))

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def _parse_ms(value: Any, default: Optional[int] = None) -> Optional[int]:
    if value is None or value == "":
        return default
    if isinstance(value, (int, float)):
        return int(value)
    text = str(value).strip()
    if text.isdigit():
        return int(text)
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except ValueError:
        raise HTTPException(400, f"无法解析时间: {value}")


def _strategy_from_payload(payload: Optional[dict]) -> StrategyConfig:
    base = store.config.strategy.model_dump()
    if payload:
        base.update({k: v for k, v in payload.items() if k in base})
    try:
        return StrategyConfig(**base)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"策略参数不合法: {exc}")


def _sync_range(
    source: str,
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    progress: Callable,
    proxy: str,
) -> dict:
    if source == "okx":
        return sync_okx(symbol, timeframe, start_ms, end_ms, progress, proxy)
    return sync_binance_archive(symbol, timeframe, start_ms, end_ms, progress, proxy)


# ---------------- basics ----------------
@app.get("/api/health")
def health():
    return {
        "ok": True,
        "version": __version__,
        "time": int(time.time() * 1000),
        "running": engine.running,
    }


@app.get("/api/exchanges")
def exchanges():
    return [
        {
            "id": key,
            "name": val["name"],
            "ccxt": val["ccxt"],
            "needs_passphrase": val["password"],
            "modes": ["paper", "testnet", "live"] if key == "binance" else ["paper", "live"],
        }
        for key, val in EXCHANGES.items()
    ]


@app.get("/api/exchanges/probe")
def exchange_probe(exchange: str = "binance", testnet: bool = False):
    if exchange not in EXCHANGES:
        raise HTTPException(400, "未知交易所")
    return probe(exchange, testnet=testnet, proxy=store.config.proxy)


@app.get("/api/symbols")
def symbols():
    return [{"value": s, "label": s.replace("/", " / ") + " 永续"} for s in POPULAR_SYMBOLS]


@app.get("/api/config")
def get_config():
    return {
        "strategy": store.config.strategy.model_dump(),
        "exchange": store.config.exchange,
        "data_source": store.config.data_source,
        "proxy": store.config.proxy,
        "default_mode": store.config.default_mode,
        "risk_ack": store.config.risk_ack,
        "timeframes": list(TIMEFRAMES.keys()),
        "keys": store.masked_secrets(),
    }


@app.put("/api/strategy")
def update_strategy(payload: dict):
    strategy = _strategy_from_payload(payload)
    store.update_strategy(strategy.model_dump())
    return {"ok": True, "strategy": store.config.strategy.model_dump()}


@app.put("/api/settings")
def update_settings(payload: dict):
    cfg = store.config.model_dump()
    for key in ("exchange", "data_source", "proxy", "default_mode", "risk_ack"):
        if key in payload:
            cfg[key] = payload[key]
    try:
        new_cfg = AppConfig(**cfg)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, f"设置不合法: {exc}")
    store.save_config(new_cfg)
    return {"ok": True}


@app.get("/api/presets")
def get_presets():
    return store.presets


@app.post("/api/presets")
def save_preset(payload: dict):
    strategy = _strategy_from_payload(payload)
    return {"ok": True, "presets": store.save_preset(strategy)}


@app.delete("/api/presets/{name}")
def remove_preset(name: str):
    return {"ok": True, "presets": store.delete_preset(name)}


# ---------------- api keys ----------------
KEY_SLOTS = {"binance", "binance_testnet", "okx"}


@app.get("/api/keys")
def get_keys():
    return store.masked_secrets()


@app.post("/api/keys")
def save_keys(payload: dict):
    exchange = payload.get("exchange")
    if exchange not in KEY_SLOTS:
        raise HTTPException(400, "未知交易所")
    current: ExchangeKey = getattr(store.secrets, exchange)
    updates = {}
    for field in ("api_key", "secret", "passphrase"):
        value = payload.get(field)
        if value is None or "*" in str(value):
            updates[field] = getattr(current, field)
        else:
            updates[field] = str(value).strip()
    merged = current.model_dump()
    merged.update(updates)
    secrets = Secrets(**{**store.secrets.model_dump(), exchange: merged})
    store.save_secrets(secrets)
    return {"ok": True, "keys": store.masked_secrets()}


@app.post("/api/keys/test")
def test_keys(payload: dict):
    exchange = payload.get("exchange", "binance")
    mode = payload.get("mode", "live")
    if exchange not in KEY_SLOTS:
        raise HTTPException(400, "未知交易所")
    key: ExchangeKey = getattr(store.secrets, exchange)
    if not key.configured:
        raise HTTPException(400, "尚未配置 API Key")
    from .broker import Broker

    real_exchange = "binance" if exchange == "binance_testnet" else exchange
    testnet = mode == "testnet" or exchange == "binance_testnet"
    try:
        broker = Broker(
            real_exchange,
            "testnet" if testnet else "live",
            keys=key.model_dump(),
            proxy=store.config.proxy,
            testnet=testnet,
        )
        broker.set_symbol(store.config.strategy.symbol)
        bal = broker.fetch_balance()
        broker.close()
        return {
            "ok": True,
            "equity": round(bal["total"], 2),
            "available": round(bal["free"], 2),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


# ---------------- data ----------------
@app.get("/api/data/inventory")
def inventory():
    return storage.candle_inventory()


@app.post("/api/data/sync")
def data_sync(payload: dict):
    source = payload.get("source", "binance")
    symbol = payload.get("symbol", "ETH/USDT")
    timeframe = payload.get("timeframe", "15m")
    if source not in ("binance", "okx"):
        raise HTTPException(400, "数据源仅支持 binance / okx")
    if timeframe not in TIMEFRAMES:
        raise HTTPException(400, "不支持的周期")
    end = _parse_ms(payload.get("end"), int(time.time() * 1000))
    start = _parse_ms(payload.get("start"), end - 365 * 86400_000)
    assert start is not None and end is not None
    if end <= start:
        raise HTTPException(400, "结束时间必须晚于开始时间")

    proxy = store.config.proxy

    def work(progress: Callable) -> dict:
        progress(3, "开始同步历史 K 线…")
        result = _sync_range(source, symbol, timeframe, start, end, progress, proxy)
        result["start"] = start
        result["end"] = end
        return result

    return {"job_id": start_job("data_sync", work)}


@app.delete("/api/data")
def data_delete(source: str, symbol: str, timeframe: str):
    storage.delete_candles(source, symbol, timeframe)
    return {"ok": True}


# ---------------- backtest ----------------
@app.post("/api/backtest")
def backtest(payload: dict):
    global LAST_BACKTEST
    source = payload.get("source", "binance")
    if source not in ("binance", "okx"):
        raise HTTPException(400, "数据源仅支持 binance / okx")
    strategy = _strategy_from_payload(payload.get("strategy"))
    symbol = strategy.symbol
    timeframe = strategy.timeframe
    end = _parse_ms(payload.get("end"), int(time.time() * 1000))
    start = _parse_ms(payload.get("start"), end - 365 * 86400_000)
    assert start is not None and end is not None
    if end <= start:
        raise HTTPException(400, "结束时间必须晚于开始时间")
    auto_fetch = bool(payload.get("auto_fetch", True))
    proxy = store.config.proxy
    step = TIMEFRAMES[timeframe]

    def work(progress: Callable) -> dict:
        progress(3, "读取本地 K 线…")
        candles = storage.get_candles(source, symbol, timeframe, start, end)
        expected = max(1, (end - start) // step)
        coverage = len(candles) / expected
        if auto_fetch and coverage < 0.8:
            progress(8, f"本地数据不足（{coverage * 100:.0f}%），正在从 {source.upper()} 同步…")
            try:
                _sync_range(
                    source,
                    symbol,
                    timeframe,
                    start,
                    end,
                    lambda p, m: progress(8 + p * 0.62, m),
                    proxy,
                )
            except DataError as exc:
                if not candles:
                    raise RuntimeError(str(exc)) from exc
                progress(70, f"同步失败，使用本地已有数据：{exc}")
            candles = storage.get_candles(source, symbol, timeframe, start, end)
        if not candles:
            raise RuntimeError("没有可用历史数据，请先在数据中心同步，或检查网络/代理设置")
        progress(72, f"开始回测 · {len(candles):,} 根 K 线")
        result = run_backtest(candles, strategy, source=source)
        progress(96, "汇总绩效指标…")
        result["requested"] = {"start": start, "end": end, "source": source}
        result["coverage_pct"] = round(len(candles) / max(1, expected) * 100, 1)
        LAST_BACKTEST = result
        return result

    job = start_job("backtest", work)
    return {"job_id": job}


@app.get("/api/backtest/last")
def last_backtest():
    if LAST_BACKTEST is None:
        return {"exists": False}
    return {"exists": True, "result": LAST_BACKTEST}


# ---------------- live ----------------
@app.get("/api/live/status")
def live_status():
    return engine.snapshot()


@app.post("/api/live/start")
def live_start(payload: dict):
    mode = payload.get("mode", "paper")
    exchange = payload.get("exchange", store.config.exchange)
    if mode == "live" and not store.config.risk_ack:
        raise HTTPException(400, "请先在设置中阅读并确认实盘风险")
    if mode == "live" and payload.get("confirm") != "START LIVE":
        raise HTTPException(400, "实盘启动需要二次确认")
    if mode in ("testnet", "live"):
        key_name = "binance_testnet" if (mode == "testnet" and exchange == "binance") else exchange
        key: ExchangeKey = getattr(store.secrets, key_name)
        if not key.configured:
            raise HTTPException(400, "请先在设置中配置该交易所的 API Key" + ("（测试网）" if key_name.endswith("testnet") else ""))
        if exchange == "okx" and not key.passphrase:
            raise HTTPException(400, "欧易 API 需要 Passphrase")
    strategy = _strategy_from_payload(payload.get("strategy"))
    store.update_strategy(strategy.model_dump())
    try:
        return engine.start(
            mode=mode,
            exchange=exchange,
            symbol=strategy.symbol,
            strategy=strategy,
            proxy=store.config.proxy,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


@app.post("/api/live/stop")
def live_stop(payload: Optional[dict] = None):
    close_position = bool((payload or {}).get("close_position", False))
    try:
        return engine.stop(close_position=close_position)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


@app.post("/api/live/panic")
def live_panic():
    try:
        return engine.panic()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(400, str(exc))


@app.get("/api/trades")
def trades(mode: Optional[str] = None, limit: int = 200):
    return storage.list_trades(mode, min(max(limit, 1), 2000))


@app.get("/api/equity")
def equity(mode: str, limit: int = 2000):
    return storage.get_equity(mode, min(max(limit, 1), 5000))


@app.get("/api/logs")
def logs(limit: int = 150):
    return storage.recent_logs(min(max(limit, 1), 500))


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job


@app.get("/api/live/events")
async def live_events():
    async def gen():
        last_payload = None
        while True:
            try:
                snap = await asyncio.to_thread(engine.snapshot)
                payload = json.dumps(snap, ensure_ascii=False, default=str)
                if payload != last_payload:
                    yield f"data: {payload}\n\n"
                    last_payload = payload
                else:
                    yield ": keep-alive\n\n"
            except asyncio.CancelledError:
                break
            except Exception as exc:  # noqa: BLE001
                yield f"event: error\ndata: {json.dumps({'error': str(exc)})}\n\n"
            await asyncio.sleep(2.0)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )


# ---------------- static ----------------
app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")


@app.get("/")
def index():
    return FileResponse(str(WEB_DIR / "index.html"))


@app.exception_handler(ValueError)
async def value_error_handler(_request, exc: ValueError):
    return JSONResponse(status_code=400, content={"error": str(exc)})


@app.on_event("startup")
def on_startup() -> None:
    storage.init_db()
    storage.add_log("info", "服务启动")
