"""
SQLite 持久化。新版增加：
- trades 表增加精细时间戳字段（t_signal / t_cex_sent / t_cex_filled / t_dex_sent / t_dex_confirmed）
- trades 表增加手续费和净PnL字段
- candidates 表增加 pool_tvl_usd、pool_fee_bps
"""
import aiosqlite
import asyncio
import json
from pathlib import Path
from typing import Optional
from datetime import datetime, timezone

DB_PATH = Path(__file__).parent.parent / "data" / "arb.db"
DB_PATH.parent.mkdir(exist_ok=True)

SCHEMA = """
CREATE TABLE IF NOT EXISTS trades (
    id INTEGER PRIMARY KEY AUTOINCREMENT,

    -- 基础信息
    ts_open  INTEGER NOT NULL,
    ts_close INTEGER,
    symbol TEXT NOT NULL,
    token_address TEXT NOT NULL,
    pool_address TEXT,
    pool_fee INTEGER,
    side TEXT NOT NULL,
    position_usdt REAL NOT NULL,
    leverage INTEGER NOT NULL,

    -- 基差
    entry_basis_pct REAL NOT NULL,
    exit_basis_pct REAL,

    -- 实际成交价
    dex_entry_price REAL,
    dex_exit_price  REAL,
    cex_entry_price REAL,
    cex_exit_price  REAL,
    dex_entry_amount REAL,     -- 买入 token 数量
    dex_exit_amount  REAL,     -- 卖出换回 USDT 数量
    cex_entry_qty    REAL,     -- 开空的合约数
    cex_exit_qty     REAL,

    -- 订单ID / 交易哈希
    dex_tx_hash_open TEXT,
    dex_tx_hash_close TEXT,
    cex_order_id_open TEXT,
    cex_order_id_close TEXT,

    -- 精细时间戳（全部 ms）
    t_signal             INTEGER,  -- 发现基差机会的时间点
    t_cex_sent_open      INTEGER,  -- CEX 开仓请求发出
    t_cex_filled_open    INTEGER,  -- CEX 开仓成交确认
    t_dex_sent_open      INTEGER,  -- DEX 开仓 sendRawTx
    t_dex_confirmed_open INTEGER,  -- DEX 开仓 receipt
    t_signal_close       INTEGER,  -- 平仓触发时间
    t_cex_sent_close     INTEGER,
    t_cex_filled_close   INTEGER,
    t_dex_sent_close     INTEGER,
    t_dex_confirmed_close INTEGER,

    -- 衍生指标（ms）
    exec_latency_ms_open  INTEGER, -- t_dex_confirmed_open - t_signal
    exec_latency_ms_close INTEGER, -- t_dex_confirmed_close - t_signal_close
    cex_fill_latency_open INTEGER, -- t_cex_filled_open - t_cex_sent_open
    dex_send_latency_open INTEGER, -- t_dex_sent_open - t_cex_filled_open
    dex_confirm_latency_open INTEGER, -- t_dex_confirmed_open - t_dex_sent_open

    -- 手续费与PnL（USDT）
    cex_fee_usdt      REAL DEFAULT 0,
    dex_fee_usdt      REAL DEFAULT 0,  -- V3 pool fee
    gas_fee_usdt      REAL DEFAULT 0,  -- BSC gas
    gross_pnl_usdt    REAL DEFAULT 0,  -- 不含费
    realized_pnl_usdt REAL,            -- 含费净利（正式PnL）

    -- 状态
    dry_run INTEGER DEFAULT 0,
    status TEXT DEFAULT 'open',
    error TEXT
);

CREATE INDEX IF NOT EXISTS ix_trades_status ON trades(status);
CREATE INDEX IF NOT EXISTS ix_trades_ts_open ON trades(ts_open);

CREATE TABLE IF NOT EXISTS candidates (
    symbol TEXT PRIMARY KEY,
    base_asset TEXT NOT NULL,
    token_address TEXT,
    pool_address TEXT,
    pool_fee INTEGER,
    pool_fee_pct REAL,           -- 0.05 等
    pool_tvl_usd REAL,
    pool_24h_vol_usd REAL,
    decimals INTEGER,
    change_24h_pct REAL,
    last_cex_price REAL,
    last_dex_price REAL,
    last_basis_pct REAL,
    source TEXT,                 -- 'binance_capital+birdeye' etc
    last_update INTEGER
);

CREATE TABLE IF NOT EXISTS latency_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    kind TEXT NOT NULL,
    ms INTEGER NOT NULL,
    symbol TEXT,
    tx_hash TEXT
);
CREATE INDEX IF NOT EXISTS ix_latency_ts ON latency_samples(ts);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    level TEXT NOT NULL,
    msg TEXT NOT NULL,
    data TEXT
);
CREATE INDEX IF NOT EXISTS ix_events_ts ON events(ts);

-- 缓存：币安BSC网络支持的币种（每天刷新一次），避免重复请求
CREATE TABLE IF NOT EXISTS binance_bsc_coins (
    coin TEXT PRIMARY KEY,             -- 币种简称，如 CAKE
    name TEXT,
    trading INTEGER,
    contract_address TEXT,             -- 可能为空，后续由scanner补充
    deposit_enable INTEGER,
    withdraw_enable INTEGER,
    last_update INTEGER
);
"""


class DB:
    _lock = asyncio.Lock()

    @staticmethod
    async def init():
        async with aiosqlite.connect(DB_PATH) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    @staticmethod
    async def execute(sql: str, params: tuple = ()) -> int:
        async with DB._lock:
            async with aiosqlite.connect(DB_PATH) as db:
                cur = await db.execute(sql, params)
                await db.commit()
                return cur.lastrowid

    @staticmethod
    async def fetchall(sql: str, params: tuple = ()) -> list:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            rows = await cur.fetchall()
            return [dict(r) for r in rows]

    @staticmethod
    async def fetchone(sql: str, params: tuple = ()) -> Optional[dict]:
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cur = await db.execute(sql, params)
            row = await cur.fetchone()
            return dict(row) if row else None

    # ---- 便捷方法 ----

    @staticmethod
    async def log_event(level: str, msg: str, data: dict | None = None):
        await DB.execute(
            "INSERT INTO events (ts, level, msg, data) VALUES (?, ?, ?, ?)",
            (int(datetime.now(timezone.utc).timestamp() * 1000),
             level, msg, json.dumps(data) if data else None)
        )

    @staticmethod
    async def log_latency(kind: str, ms: int, symbol: str = "", tx: str = ""):
        await DB.execute(
            "INSERT INTO latency_samples (ts, kind, ms, symbol, tx_hash) VALUES (?,?,?,?,?)",
            (int(datetime.now(timezone.utc).timestamp() * 1000), kind, ms, symbol, tx)
        )

    @staticmethod
    async def upsert_candidate(c: dict):
        await DB.execute(
            """INSERT OR REPLACE INTO candidates
            (symbol, base_asset, token_address, pool_address, pool_fee, pool_fee_pct,
             pool_tvl_usd, pool_24h_vol_usd, decimals, change_24h_pct,
             last_cex_price, last_dex_price, last_basis_pct, source, last_update)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (c.get("symbol"), c.get("base_asset"), c.get("token_address"),
             c.get("pool_address"), c.get("pool_fee"), c.get("pool_fee_pct"),
             c.get("pool_tvl_usd"), c.get("pool_24h_vol_usd"),
             c.get("decimals"), c.get("change_24h_pct"),
             c.get("last_cex_price"), c.get("last_dex_price"),
             c.get("last_basis_pct"), c.get("source", ""),
             int(datetime.now(timezone.utc).timestamp() * 1000))
        )

    @staticmethod
    async def clear_candidates():
        await DB.execute("DELETE FROM candidates", ())

    @staticmethod
    async def cache_binance_bsc_coins(coins: list[dict]):
        now = int(datetime.now(timezone.utc).timestamp() * 1000)
        async with DB._lock:
            async with aiosqlite.connect(DB_PATH) as db:
                await db.execute("DELETE FROM binance_bsc_coins")
                for c in coins:
                    await db.execute(
                        """INSERT OR REPLACE INTO binance_bsc_coins
                        (coin, name, trading, contract_address, deposit_enable, withdraw_enable, last_update)
                        VALUES (?,?,?,?,?,?,?)""",
                        (c["coin"], c.get("name", ""), 1 if c.get("trading") else 0,
                         c.get("contract_address", ""),
                         1 if c.get("deposit_enable") else 0,
                         1 if c.get("withdraw_enable") else 0, now)
                    )
                await db.commit()
