from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Iterable, List, Optional, Sequence, Tuple

from .config import DB_PATH, DATA_DIR

_init_lock = threading.Lock()
_initialized = False


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    return conn


def init_db() -> None:
    global _initialized
    with _init_lock:
        if _initialized:
            return
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with _connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS candles (
                    source TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    timeframe TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    open REAL NOT NULL,
                    high REAL NOT NULL,
                    low REAL NOT NULL,
                    close REAL NOT NULL,
                    volume REAL NOT NULL DEFAULT 0,
                    PRIMARY KEY (source, symbol, timeframe, ts)
                );
                CREATE INDEX IF NOT EXISTS idx_candles_range
                    ON candles(source, symbol, timeframe, ts);

                CREATE TABLE IF NOT EXISTS trades (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    mode TEXT NOT NULL,
                    exchange TEXT,
                    symbol TEXT,
                    side INTEGER,
                    entry_ts INTEGER,
                    exit_ts INTEGER,
                    entry REAL,
                    exit REAL,
                    qty REAL,
                    notional REAL,
                    pnl REAL,
                    pnl_pct REAL,
                    fees REAL,
                    funding REAL,
                    success INTEGER,
                    reason TEXT,
                    created_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_trades_mode ON trades(mode, exit_ts);

                CREATE TABLE IF NOT EXISTS equity (
                    mode TEXT NOT NULL,
                    ts INTEGER NOT NULL,
                    equity REAL NOT NULL,
                    PRIMARY KEY (mode, ts)
                );

                CREATE TABLE IF NOT EXISTS live_state (
                    id INTEGER PRIMARY KEY CHECK (id = 1),
                    payload TEXT NOT NULL,
                    updated_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    ts INTEGER NOT NULL,
                    level TEXT NOT NULL,
                    mode TEXT,
                    message TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_logs_ts ON logs(ts);
                """
            )
        _initialized = True


# ---------------- candles ----------------

def upsert_candles(
    source: str,
    symbol: str,
    timeframe: str,
    rows: Iterable[Sequence[float]],
) -> int:
    init_db()
    payload = [
        (source, symbol, timeframe, int(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[4]), float(r[5]))
        for r in rows
    ]
    if not payload:
        return 0
    with _connect() as conn:
        before = conn.total_changes
        conn.executemany(
            """INSERT OR REPLACE INTO candles
               (source, symbol, timeframe, ts, open, high, low, close, volume)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        return conn.total_changes - before


def get_candles(
    source: str,
    symbol: str,
    timeframe: str,
    start_ms: Optional[int] = None,
    end_ms: Optional[int] = None,
) -> List[Tuple[int, float, float, float, float, float]]:
    init_db()
    sql = "SELECT ts, open, high, low, close, volume FROM candles WHERE source=? AND symbol=? AND timeframe=?"
    args: list = [source, symbol, timeframe]
    if start_ms is not None:
        sql += " AND ts>=?"
        args.append(int(start_ms))
    if end_ms is not None:
        sql += " AND ts<?"
        args.append(int(end_ms))
    sql += " ORDER BY ts ASC"
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [(r["ts"], r["open"], r["high"], r["low"], r["close"], r["volume"]) for r in rows]


def candle_inventory() -> List[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            """SELECT source, symbol, timeframe, COUNT(*) AS count,
                      MIN(ts) AS start_ts, MAX(ts) AS end_ts
               FROM candles GROUP BY source, symbol, timeframe
               ORDER BY source, symbol, timeframe"""
        ).fetchall()
    return [
        {
            "source": r["source"],
            "symbol": r["symbol"],
            "timeframe": r["timeframe"],
            "count": r["count"],
            "start": r["start_ts"],
            "end": r["end_ts"],
        }
        for r in rows
    ]


def delete_candles(source: str, symbol: str, timeframe: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "DELETE FROM candles WHERE source=? AND symbol=? AND timeframe=?",
            (source, symbol, timeframe),
        )


# ---------------- trades / equity ----------------

def add_trade(mode: str, trade: dict) -> int:
    init_db()
    with _connect() as conn:
        cur = conn.execute(
            """INSERT INTO trades
               (mode, exchange, symbol, side, entry_ts, exit_ts, entry, exit, qty,
                notional, pnl, pnl_pct, fees, funding, success, reason, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                mode,
                trade.get("exchange"),
                trade.get("symbol"),
                trade.get("side"),
                trade.get("entry_ts"),
                trade.get("exit_ts"),
                trade.get("entry"),
                trade.get("exit"),
                trade.get("qty"),
                trade.get("notional"),
                trade.get("pnl"),
                trade.get("pnl_pct"),
                trade.get("fees"),
                trade.get("funding"),
                1 if trade.get("success") else 0,
                trade.get("reason"),
                int(time.time() * 1000),
            ),
        )
        return int(cur.lastrowid)


def list_trades(mode: Optional[str] = None, limit: int = 200) -> List[dict]:
    init_db()
    sql = "SELECT * FROM trades"
    args: list = []
    if mode:
        sql += " WHERE mode=?"
        args.append(mode)
    sql += " ORDER BY exit_ts DESC LIMIT ?"
    args.append(limit)
    with _connect() as conn:
        rows = conn.execute(sql, args).fetchall()
    return [dict(r) for r in rows]


def add_equity(mode: str, ts: int, equity: float) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO equity(mode, ts, equity) VALUES (?,?,?)",
            (mode, int(ts), float(equity)),
        )


def get_equity(mode: str, limit: int = 2000) -> List[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, equity FROM equity WHERE mode=? ORDER BY ts DESC LIMIT ?",
            (mode, limit),
        ).fetchall()
    return [{"ts": r["ts"], "equity": r["equity"]} for r in reversed(rows)]


def reset_mode(mode: str) -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM trades WHERE mode=?", (mode,))
        conn.execute("DELETE FROM equity WHERE mode=?", (mode,))


# ---------------- live state / logs ----------------

def save_live_state(payload: dict) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO live_state(id, payload, updated_at) VALUES (1, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
            (json.dumps(payload, ensure_ascii=False), int(time.time() * 1000)),
        )


def load_live_state() -> Optional[dict]:
    init_db()
    with _connect() as conn:
        row = conn.execute("SELECT payload FROM live_state WHERE id=1").fetchone()
    if not row:
        return None
    try:
        return json.loads(row["payload"])
    except Exception:
        return None


def clear_live_state() -> None:
    init_db()
    with _connect() as conn:
        conn.execute("DELETE FROM live_state WHERE id=1")


def add_log(level: str, message: str, mode: Optional[str] = None) -> None:
    init_db()
    with _connect() as conn:
        conn.execute(
            "INSERT INTO logs(ts, level, mode, message) VALUES (?,?,?,?)",
            (int(time.time() * 1000), level, mode, message),
        )
        conn.execute(
            "DELETE FROM logs WHERE id NOT IN (SELECT id FROM logs ORDER BY id DESC LIMIT 800)"
        )


def recent_logs(limit: int = 120) -> List[dict]:
    init_db()
    with _connect() as conn:
        rows = conn.execute(
            "SELECT ts, level, mode, message FROM logs ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in reversed(rows)]
