from __future__ import annotations

import csv
import io
import os
import time
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

import httpx

from . import storage
from .config import CACHE_DIR, TIMEFRAMES

ARCHIVE_BASE = "https://data.binance.vision/data/futures/um"
Progress = Callable[[float, str], None]


class DataError(RuntimeError):
    pass


def ccxt_symbol(symbol: str) -> str:
    """ETH/USDT -> ETH/USDT:USDT（永续）"""
    if ":" in symbol:
        return symbol
    base, quote = symbol.split("/")
    return f"{base}/{quote}:{quote}"


def ui_symbol(symbol: str) -> str:
    return symbol.split(":")[0]


def archive_symbol(symbol: str) -> str:
    return ui_symbol(symbol).replace("/", "")


def okx_inst(symbol: str) -> str:
    base, quote = ui_symbol(symbol).split("/")
    return f"{base}-{quote}-SWAP"


def resolve_proxy(proxy: str = "") -> str:
    """ccxt 不会自动读取 HTTPS_PROXY 环境变量，这里显式解析。"""
    if proxy:
        return proxy
    return (
        os.environ.get("HTTPS_PROXY")
        or os.environ.get("https_proxy")
        or os.environ.get("HTTP_PROXY")
        or os.environ.get("http_proxy")
        or ""
    )


def _proxy_client(proxy: str = "") -> httpx.Client:
    kwargs = {"timeout": httpx.Timeout(40.0), "follow_redirects": True, "trust_env": True}
    if proxy:
        kwargs["proxy"] = proxy
    try:
        return httpx.Client(**kwargs)
    except TypeError:  # 兼容旧版 httpx
        kwargs.pop("proxy", None)
        kwargs["proxies"] = proxy
        return httpx.Client(**kwargs)


def _download(client: httpx.Client, url: str, dest: Path, retries: int = 3) -> Optional[Path]:
    if dest.exists() and dest.stat().st_size > 500:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    last_error: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with client.stream("GET", url) as resp:
                if resp.status_code == 404:
                    return None
                resp.raise_for_status()
                tmp = dest.with_suffix(dest.suffix + ".part")
                with open(tmp, "wb") as fh:
                    for chunk in resp.iter_bytes(chunk_size=1 << 16):
                        fh.write(chunk)
                tmp.replace(dest)
                return dest
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            time.sleep(0.8 * (attempt + 1))
    raise DataError(f"下载失败 {url}: {last_error}")


def _parse_zip_rows(path: Path) -> List[Tuple[int, float, float, float, float, float]]:
    rows: List[Tuple[int, float, float, float, float, float]] = []
    with zipfile.ZipFile(path) as zf:
        name = zf.namelist()[0]
        with zf.open(name) as raw:
            text = io.TextIOWrapper(raw, encoding="utf-8")
            reader = csv.reader(text)
            for row in reader:
                if not row or not row[0] or not row[0].strip().lstrip("-").isdigit():
                    continue
                ts = int(row[0])
                if ts > 10**15:  # 微秒 -> 毫秒
                    ts //= 1000
                try:
                    rows.append((ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
                except (ValueError, IndexError):
                    continue
    rows.sort(key=lambda r: r[0])
    return rows


def _month_key(dt: datetime) -> str:
    return dt.strftime("%Y-%m")


def sync_binance_archive(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    progress: Optional[Progress] = None,
    proxy: str = "",
) -> dict:
    """从 Binance 公共数据归档下载 UM 永续 K 线（无需 API Key，不受区域 API 限制）。"""
    if timeframe not in TIMEFRAMES:
        raise DataError(f"不支持的周期: {timeframe}")
    if end_ms <= start_ms:
        raise DataError("结束时间必须晚于开始时间")

    sym = archive_symbol(symbol)
    ui_sym = ui_symbol(symbol)
    now = datetime.now(timezone.utc)
    start_dt = datetime.fromtimestamp(start_ms / 1000, tz=timezone.utc).replace(minute=0, second=0, microsecond=0)
    end_dt = datetime.fromtimestamp(end_ms / 1000, tz=timezone.utc)
    if end_dt > now:
        end_dt = now

    # 每个月：整月用 monthly，当月/最新缺口用 daily
    units: List[Tuple[str, str]] = []  # (kind, key)
    cursor = start_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    while cursor <= end_dt:
        next_month = (cursor.replace(day=28) + timedelta(days=4)).replace(day=1)
        month_end = (next_month - timedelta(days=1)).date()
        if month_end < start_dt.date() or cursor.date() > end_dt.date():
            cursor = next_month
            continue
        is_complete = month_end < (now.date() - timedelta(days=1))
        if is_complete:
            units.append(("monthly", _month_key(cursor)))
        else:
            day = max(cursor.date(), start_dt.date())
            last_day = min(month_end, end_dt.date() - timedelta(days=1), now.date() - timedelta(days=1))
            while day <= last_day:
                units.append(("daily", day.isoformat()))
                day += timedelta(days=1)
        cursor = next_month

    if not units:
        raise DataError("所选区间暂无可用的历史归档数据（归档通常延迟 1 天）")

    total = 0
    fetched = 0
    latest_ts = 0
    with _proxy_client(proxy) as client:
        for idx, (kind, key) in enumerate(units):
            filename = f"{sym}-{timeframe}-{key}.zip"
            if kind == "monthly":
                url = f"{ARCHIVE_BASE}/monthly/klines/{sym}/{timeframe}/{filename}"
            else:
                url = f"{ARCHIVE_BASE}/daily/klines/{sym}/{timeframe}/{filename}"
            dest = CACHE_DIR / "binance" / sym / timeframe / filename
            got = _download(client, url, dest)
            if got is None:
                if progress:
                    progress((idx + 1) / len(units) * 100, f"跳过 {key}（暂无归档）")
                continue
            rows = _parse_zip_rows(got)
            rows = [r for r in rows if start_ms <= r[0] < end_ms]
            if rows:
                total += storage.upsert_candles("binance", ui_sym, timeframe, rows)
                latest_ts = max(latest_ts, rows[-1][0])
                fetched += len(rows)
            if progress:
                progress(
                    (idx + 1) / len(units) * 100,
                    f"已处理 {idx + 1}/{len(units)} · {key} · 累计 {total:,} 条",
                )

    return {
        "source": "binance",
        "symbol": ui_sym,
        "timeframe": timeframe,
        "inserted": total,
        "downloaded_rows": fetched,
        "latest_ts": latest_ts,
        "units": len(units),
    }


def sync_okx(
    symbol: str,
    timeframe: str,
    start_ms: int,
    end_ms: int,
    progress: Optional[Progress] = None,
    proxy: str = "",
) -> dict:
    """通过 OKX 公共 REST 拉取永续 K 线（需要网络可访问 okx.com）。"""
    import ccxt

    if timeframe not in TIMEFRAMES:
        raise DataError(f"不支持的周期: {timeframe}")
    ui_sym = ui_symbol(symbol)
    step = TIMEFRAMES[timeframe]
    opts = {"enableRateLimit": True, "timeout": 30000, "options": {"defaultType": "swap"}}
    proxy = resolve_proxy(proxy)
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    ex = ccxt.okx(opts)
    try:
        ex.load_markets()
    except Exception as exc:  # noqa: BLE001
        raise DataError(
            f"无法连接 OKX 公共接口（{type(exc).__name__}）。当前网络可能限制了 okx.com，"
            "可在“设置”中配置代理后重试。"
        ) from exc

    target = ccxt_symbol(symbol)
    since = int(start_ms)
    inserted = 0
    latest = 0
    requests = 0
    buffer: list = []
    while since < end_ms and requests < 20000:
        try:
            batch = ex.fetch_ohlcv(target, timeframe, since=since, limit=100)
        except Exception as exc:  # noqa: BLE001
            raise DataError(f"OKX 数据拉取失败: {exc}") from exc
        requests += 1
        if not batch:
            break
        for row in batch:
            ts = int(row[0])
            if start_ms <= ts < end_ms:
                buffer.append((ts, float(row[1]), float(row[2]), float(row[3]), float(row[4]), float(row[5])))
        latest = max(latest, int(batch[-1][0]))
        since = int(batch[-1][0]) + step
        if len(buffer) >= 5000:
            inserted += storage.upsert_candles("okx", ui_sym, timeframe, buffer)
            buffer.clear()
        if progress and requests % 5 == 0:
            pct = min(99.0, (since - start_ms) / max(1, end_ms - start_ms) * 100)
            progress(pct, f"OKX 已拉取 {inserted + len(buffer):,} 条 · 请求 {requests}")
        if batch[-1][0] >= end_ms - step:
            break
    if buffer:
        inserted += storage.upsert_candles("okx", ui_sym, timeframe, buffer)

    return {
        "source": "okx",
        "symbol": ui_sym,
        "timeframe": timeframe,
        "inserted": inserted,
        "downloaded_rows": inserted,
        "latest_ts": latest,
        "units": requests,
    }


def fetch_recent_ohlcv(
    exchange_id: str,
    symbol: str,
    timeframe: str,
    limit: int = 500,
    testnet: bool = False,
    proxy: str = "",
    keys: Optional[dict] = None,
) -> List[list]:
    """通过 ccxt 拉取近期 K 线（用于实时引擎预热）。"""
    import ccxt

    ex_cls = getattr(ccxt, "binanceusdm" if exchange_id == "binance" else exchange_id)
    opts = {
        "enableRateLimit": True,
        "timeout": 20000,
        "options": {"defaultType": "swap"},
    }
    proxy = resolve_proxy(proxy)
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    if keys and keys.get("api_key"):
        opts["apiKey"] = keys["api_key"]
        opts["secret"] = keys.get("secret", "")
        if keys.get("passphrase"):
            opts["password"] = keys["passphrase"]
    ex = ex_cls(opts)
    try:
        if testnet:
            ex.set_sandbox_mode(True)
        ex.load_markets()
        effective = ex.safe_symbol(symbol) if hasattr(ex, "safe_symbol") else symbol
        return ex.fetch_ohlcv(effective, timeframe, limit=limit)
    finally:
        try:
            ex.close()
        except Exception:
            pass


def fetch_ticker_price(
    exchange_id: str,
    symbol: str,
    testnet: bool = False,
    proxy: str = "",
) -> Optional[float]:
    import ccxt

    ex_cls = getattr(ccxt, "binanceusdm" if exchange_id == "binance" else exchange_id)
    opts = {"enableRateLimit": True, "timeout": 15000, "options": {"defaultType": "swap"}}
    proxy = resolve_proxy(proxy)
    if proxy:
        opts["proxies"] = {"http": proxy, "https": proxy}
    ex = ex_cls(opts)
    try:
        if testnet:
            ex.set_sandbox_mode(True)
        ex.load_markets()
        t = ex.fetch_ticker(ccxt_symbol(symbol))
        return float(t.get("last") or 0) or None
    finally:
        try:
            ex.close()
        except Exception:
            pass


def probe(exchange_id: str, testnet: bool = False, proxy: str = "") -> dict:
    """探测交易所公共接口连通性。"""
    started = time.time()
    try:
        price = fetch_ticker_price(exchange_id, "ETH/USDT", testnet=testnet, proxy=proxy)
        return {
            "ok": bool(price),
            "latency_ms": int((time.time() - started) * 1000),
            "price": price,
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "latency_ms": int((time.time() - started) * 1000),
            "error": f"{type(exc).__name__}: {str(exc)[:240]}",
        }
