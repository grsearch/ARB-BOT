"""
FastAPI Dashboard:
- GET /          -> index.html
- GET /api/config -> 当前运行时参数
- POST /api/config -> 修改参数
- GET /api/stats -> 综合统计（含净PnL、手续费、延迟分阶段分位）
- GET /api/trades -> 交易历史（含所有时间戳和手续费）
- GET /api/candidates -> 候选池（含TVL/fee/实时价）
- GET /api/positions -> 实时持仓
- GET /api/events -> 事件日志
- WS  /ws        -> 实时推送
- POST /api/close_all / /api/toggle_enabled -> 控制
"""
import json
import asyncio
from pathlib import Path
from typing import Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel
from .config import RUNTIME, STATIC
from .db import DB


FRONTEND_DIR = Path(__file__).parent.parent / "frontend"


class ConfigUpdate(BaseModel):
    entry_threshold: float | None = None
    exit_threshold: float | None = None
    position_usdt: float | None = None
    max_slippage: float | None = None
    leverage: int | None = None
    max_concurrent_positions: int | None = None
    max_exec_latency_ms: int | None = None
    dry_run: bool | None = None
    scan_interval_sec: int | None = None
    top_n_gainers: int | None = None
    min_24h_gain_pct: float | None = None
    min_pool_tvl_usd: float | None = None
    max_pool_fee_bps: int | None = None
    enabled: bool | None = None
    cex_taker_fee: float | None = None
    cex_maker_fee: float | None = None


class WSHub:
    def __init__(self):
        self.clients: Set[WebSocket] = set()
        self._lock = asyncio.Lock()

    async def connect(self, ws: WebSocket):
        await ws.accept()
        async with self._lock:
            self.clients.add(ws)

    async def disconnect(self, ws: WebSocket):
        async with self._lock:
            self.clients.discard(ws)

    async def broadcast(self, msg: dict):
        data = json.dumps(msg)
        async with self._lock:
            dead = []
            for c in self.clients:
                try:
                    await c.send_text(data)
                except Exception:
                    dead.append(c)
            for c in dead:
                self.clients.discard(c)


def _percentile(vals: list[int], pct: float) -> int:
    if not vals:
        return 0
    vals = sorted(vals)
    i = min(len(vals) - 1, int(len(vals) * pct))
    return int(vals[i])


def build_app(engine_ref: dict, hub: WSHub) -> FastAPI:
    app = FastAPI(title="Arb Bot Dashboard")

    @app.get("/")
    async def index():
        return FileResponse(FRONTEND_DIR / "index.html")

    @app.get("/api/config")
    async def get_config():
        return RUNTIME.to_dict()

    @app.post("/api/config")
    async def update_config(upd: ConfigUpdate):
        for k, v in upd.dict(exclude_unset=True).items():
            if hasattr(RUNTIME, k):
                setattr(RUNTIME, k, v)
        await DB.log_event("info", f"Config updated: {upd.dict(exclude_unset=True)}")
        return RUNTIME.to_dict()

    @app.get("/api/stats")
    async def stats():
        # 汇总
        row_closed = await DB.fetchone(
            "SELECT COUNT(*) AS n, "
            "COALESCE(SUM(realized_pnl_usdt),0) AS net_pnl, "
            "COALESCE(SUM(gross_pnl_usdt),0) AS gross_pnl, "
            "COALESCE(SUM(cex_fee_usdt),0) AS cex_fees, "
            "COALESCE(SUM(dex_fee_usdt),0) AS dex_fees, "
            "COALESCE(SUM(gas_fee_usdt),0) AS gas_fees, "
            "COALESCE(AVG(exec_latency_ms_open),0) AS avg_latency_open, "
            "COALESCE(AVG(exec_latency_ms_close),0) AS avg_latency_close "
            "FROM trades WHERE status='closed'"
        )
        row_open = await DB.fetchone("SELECT COUNT(*) AS n FROM trades WHERE status='open'")
        row_err = await DB.fetchone("SELECT COUNT(*) AS n FROM trades WHERE status='error'")
        row_win = await DB.fetchone(
            "SELECT COUNT(*) AS n FROM trades WHERE status='closed' AND realized_pnl_usdt > 0"
        )
        row_daily = await DB.fetchall(
            "SELECT DATE(ts_open/1000, 'unixepoch') AS day, "
            "COUNT(*) AS n, COALESCE(SUM(realized_pnl_usdt),0) AS pnl "
            "FROM trades WHERE status='closed' GROUP BY day ORDER BY day DESC LIMIT 14"
        )
        # 最近200笔的延迟分位数（分阶段）
        recent_trades = await DB.fetchall(
            "SELECT exec_latency_ms_open, cex_fill_latency_open, "
            "dex_send_latency_open, dex_confirm_latency_open "
            "FROM trades WHERE status!='open' ORDER BY id DESC LIMIT 200"
        )
        total_list = [t["exec_latency_ms_open"] for t in recent_trades if t["exec_latency_ms_open"]]
        cex_list = [t["cex_fill_latency_open"] for t in recent_trades if t["cex_fill_latency_open"]]
        dexsend_list = [t["dex_send_latency_open"] for t in recent_trades if t["dex_send_latency_open"]]
        dexconf_list = [t["dex_confirm_latency_open"] for t in recent_trades if t["dex_confirm_latency_open"]]

        return {
            "total_trades": row_closed["n"],
            "open_trades": row_open["n"],
            "error_trades": row_err["n"],
            "net_pnl": round(row_closed["net_pnl"] or 0, 4),
            "gross_pnl": round(row_closed["gross_pnl"] or 0, 4),
            "total_fees": round(
                (row_closed["cex_fees"] or 0) +
                (row_closed["dex_fees"] or 0) +
                (row_closed["gas_fees"] or 0), 4
            ),
            "cex_fees": round(row_closed["cex_fees"] or 0, 4),
            "dex_fees": round(row_closed["dex_fees"] or 0, 4),
            "gas_fees": round(row_closed["gas_fees"] or 0, 4),
            "win_rate": (row_win["n"] / row_closed["n"]) if row_closed["n"] else 0,
            "avg_latency_open_ms": int(row_closed["avg_latency_open"] or 0),
            "avg_latency_close_ms": int(row_closed["avg_latency_close"] or 0),
            "latency": {
                "total_p50": _percentile(total_list, 0.5),
                "total_p90": _percentile(total_list, 0.9),
                "total_p99": _percentile(total_list, 0.99),
                "cex_fill_p50": _percentile(cex_list, 0.5),
                "cex_fill_p90": _percentile(cex_list, 0.9),
                "dex_send_p50": _percentile(dexsend_list, 0.5),
                "dex_send_p90": _percentile(dexsend_list, 0.9),
                "dex_confirm_p50": _percentile(dexconf_list, 0.5),
                "dex_confirm_p90": _percentile(dexconf_list, 0.9),
            },
            "daily_pnl": row_daily,
        }

    @app.get("/api/trades")
    async def trades(limit: int = 100):
        return await DB.fetchall(
            "SELECT * FROM trades ORDER BY ts_open DESC LIMIT ?", (limit,)
        )

    @app.get("/api/candidates")
    async def candidates():
        rows = await DB.fetchall(
            "SELECT * FROM candidates ORDER BY change_24h_pct DESC"
        )
        eng = engine_ref.get("engine")
        for r in rows:
            sym = r["symbol"]
            if eng:
                cex = eng.prices_cex.get(sym, {})
                dex = eng.prices_dex.get(sym, {})
                r["cex_mid_live"] = cex.get("mid")
                r["dex_live"] = dex.get("price")
                r["dex_source"] = dex.get("source")
                if cex.get("mid") and dex.get("price") and dex["price"] > 0:
                    r["basis_live_pct"] = (cex["mid"] - dex["price"]) / dex["price"] * 100
                else:
                    r["basis_live_pct"] = None
        return rows

    @app.get("/api/events")
    async def events(limit: int = 100):
        return await DB.fetchall(
            "SELECT * FROM events ORDER BY ts DESC LIMIT ?", (limit,)
        )

    @app.get("/api/positions")
    async def positions():
        eng = engine_ref.get("engine")
        if not eng:
            return []
        out = []
        for s, p in eng.positions.items():
            cex = eng.prices_cex.get(s, {})
            dex = eng.prices_dex.get(s, {})
            cex_now = cex.get("mid")
            dex_now = dex.get("price")
            unreal_gross = 0
            unreal_net = 0
            if cex_now and dex_now:
                dex_leg = (dex_now - p.dex_entry_price) * p.dex_amount_token
                cex_leg = (p.cex_avg_entry - cex_now) * p.cex_filled_qty
                unreal_gross = dex_leg + cex_leg
                # 估算平仓手续费（以当前价格）
                cex_close_fee_est = cex_now * p.cex_filled_qty * RUNTIME.cex_taker_fee
                dex_close_fee_est = (dex_now * p.dex_amount_token) * p.pool_fee_pct
                gas_close_est = p.gas_fee_open_usdt  # 和开仓相当
                total_fees = (p.cex_fee_open_usdt + p.dex_fee_open_usdt + p.gas_fee_open_usdt
                              + cex_close_fee_est + dex_close_fee_est + gas_close_est)
                unreal_net = unreal_gross - total_fees

            out.append({
                "symbol": s,
                "ts_open": p.ts_open,
                "entry_basis_pct": p.entry_basis * 100,
                "cex_avg_entry": p.cex_avg_entry,
                "dex_entry_price": p.dex_entry_price,
                "cex_mid_now": cex_now,
                "dex_now": dex_now,
                "latency_ms_open": p.t_dex_confirmed_open - p.t_signal,
                "cex_fill_ms": p.t_cex_filled_open - p.t_cex_sent_open,
                "dex_send_ms": p.t_dex_sent_open - p.t_cex_filled_open,
                "dex_confirm_ms": p.t_dex_confirmed_open - p.t_dex_sent_open,
                "fees_paid_usdt": round(p.cex_fee_open_usdt + p.dex_fee_open_usdt + p.gas_fee_open_usdt, 4),
                "unrealized_gross": round(unreal_gross, 4),
                "unrealized_net": round(unreal_net, 4),
                "dex_tx_open": p.dex_tx_open,
            })
        return out

    @app.post("/api/close_all")
    async def close_all():
        eng = engine_ref.get("engine")
        if not eng:
            raise HTTPException(503, "Engine not running")
        await eng.force_close_all("manual")
        return {"ok": True}

    @app.post("/api/toggle_enabled")
    async def toggle_enabled():
        RUNTIME.enabled = not RUNTIME.enabled
        await DB.log_event("info", f"enabled -> {RUNTIME.enabled}")
        return {"enabled": RUNTIME.enabled}

    @app.websocket("/ws")
    async def websocket_endpoint(ws: WebSocket):
        await hub.connect(ws)
        try:
            await ws.send_text(json.dumps({
                "type": "init", "config": RUNTIME.to_dict(),
            }))
            while True:
                await ws.receive_text()
        except WebSocketDisconnect:
            await hub.disconnect(ws)
        except Exception:
            await hub.disconnect(ws)

    return app
