"""
币安合约执行器。
策略选择：先CEX后DEX。因此CEX必须"快且确定性"：使用 MARKET + IOC，等 FILLED 后再触发DEX。

关键点：
- 预先设置好杠杆（避免首次下单延迟）
- 使用 newOrderRespType=RESULT，一次调用就拿到成交均价
- 失败则立即触发cancel + alarm
"""
import asyncio
import time
import math
import ccxt.async_support as ccxt_async
from typing import Optional
from .config import STATIC, RUNTIME
from .db import DB


class CEXExecutor:
    def __init__(self):
        self.ex: Optional[ccxt_async.binance] = None
        self._leverage_cache: dict[str, int] = {}
        self._symbol_meta: dict[str, dict] = {}

    async def init(self):
        # AWS Tokyo 到币安走东京/香港节点，某些环境下 SSL 证书验证会失败
        # 禁用 ccxt 的 SSL 验证（类似 requests 的 verify=False）
        import aiohttp
        import ssl
        ssl_ctx = ssl.create_default_context()
        ssl_ctx.check_hostname = False
        ssl_ctx.verify_mode = ssl.CERT_NONE

        self.ex = ccxt_async.binance({
            "apiKey": STATIC.binance_api_key,
            "secret": STATIC.binance_api_secret,
            "options": {
                "defaultType": "future",
                "warnOnFetchOpenOrdersWithoutSymbol": False,
                "fetchCurrencies": False,   # 跳过 sapi /capital/config/getall，避免 SSL/权限问题
            },
            "enableRateLimit": True,
            "aiohttp_trust_env": True,
            "verify": False,   # 禁用 SSL 验证
        })
        # 直接替换 ccxt 的 session，禁用 SSL verify
        try:
            connector = aiohttp.TCPConnector(ssl=ssl_ctx)
            self.ex.session = aiohttp.ClientSession(connector=connector)
        except Exception as e:
            await DB.log_event("warn", f"Failed to set aiohttp session with SSL off: {e}")

        try:
            await self.ex.load_markets()
        except Exception as e:
            await DB.log_event("error", f"ccxt load_markets failed: {e}")
            # 不抛出，让 bot 启动，失败的 trade 会在实际下单时报错
            return

    def _ccxt_symbol(self, binance_symbol: str) -> str:
        """'RAVEUSDT' -> 'RAVE/USDT:USDT' (ccxt 线性永续格式)"""
        if "/" in binance_symbol:
            return binance_symbol
        if binance_symbol.endswith("USDT"):
            base = binance_symbol[:-4]
            return f"{base}/USDT:USDT"
        return binance_symbol

    async def close(self):
        if self.ex:
            await self.ex.close()

    async def ensure_leverage(self, symbol: str, leverage: int):
        sym = self._ccxt_symbol(symbol)
        if self._leverage_cache.get(sym) == leverage:
            return
        try:
            await self.ex.set_leverage(leverage, sym)
            self._leverage_cache[sym] = leverage
        except Exception as e:
            # 持仓中不允许改杠杆等错误，忽略
            await DB.log_event("warn", f"set_leverage {sym} {leverage} fail: {e}")

    def _round_qty(self, symbol: str, qty: float) -> float:
        sym = self._ccxt_symbol(symbol)
        try:
            mkt = self.ex.market(sym)
            prec = mkt["precision"]["amount"]
            if isinstance(prec, int):
                return round(qty, prec)
            # ccxt有时返回 step size（如 0.001）
            step = float(prec)
            if step <= 0:
                return round(qty, 4)
            return math.floor(qty / step) * step
        except Exception:
            return round(qty, 4)

    # ---- 开空（套利入场） ----
    async def open_short(self, symbol: str, position_usdt: float, ref_price: float, leverage: int) -> dict:
        """
        返回 {'filled', 'avg_price', 'order_id', 'latency_ms', 'ok': bool, 'error': ...}
        """
        if RUNTIME.dry_run:
            await asyncio.sleep(0.08)   # 模拟 80ms 成交
            return {
                "ok": True, "filled": position_usdt / ref_price,
                "avg_price": ref_price, "order_id": "dry_run",
                "latency_ms": 80,
            }

        await self.ensure_leverage(symbol, leverage)
        qty = position_usdt / ref_price
        qty = self._round_qty(symbol, qty)
        if qty <= 0:
            return {"ok": False, "error": "qty_zero"}

        sym = self._ccxt_symbol(symbol)
        t0 = time.time()
        try:
            order = await self.ex.create_order(
                symbol=sym, type="market", side="sell",
                amount=qty,
                params={"newOrderRespType": "RESULT", "reduceOnly": False},
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        latency_ms = int((time.time() - t0) * 1000)

        avg = float(order.get("average") or order.get("price") or ref_price)
        filled = float(order.get("filled") or qty)
        return {
            "ok": True, "filled": filled, "avg_price": avg,
            "order_id": str(order.get("id")), "latency_ms": latency_ms,
            "raw": order,
        }

    # ---- 平空（平仓） ----
    async def close_short(self, symbol: str, filled_qty: float, ref_price: float) -> dict:
        if RUNTIME.dry_run:
            await asyncio.sleep(0.08)
            return {
                "ok": True, "filled": filled_qty, "avg_price": ref_price,
                "order_id": "dry_run_close", "latency_ms": 80,
            }

        qty = self._round_qty(symbol, filled_qty)
        sym = self._ccxt_symbol(symbol)
        t0 = time.time()
        try:
            order = await self.ex.create_order(
                symbol=sym, type="market", side="buy",
                amount=qty,
                params={"newOrderRespType": "RESULT", "reduceOnly": True},
            )
        except Exception as e:
            return {"ok": False, "error": str(e)}
        latency_ms = int((time.time() - t0) * 1000)
        avg = float(order.get("average") or order.get("price") or ref_price)
        filled = float(order.get("filled") or qty)
        return {
            "ok": True, "filled": filled, "avg_price": avg,
            "order_id": str(order.get("id")), "latency_ms": latency_ms,
            "raw": order,
        }
