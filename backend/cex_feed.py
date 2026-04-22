"""
币安合约 WebSocket 价格流。
使用 !bookTicker 频道获取买一卖一，平均作为mid价（延迟<50ms）。
动态管理订阅：扫描结果变化时，更新订阅列表。
"""
import asyncio
import json
import time
import websockets
from typing import Callable, Optional, Awaitable
from .config import BINANCE_FUTURES_WS
from .db import DB

PriceCallback = Callable[[str, float, float, int], Awaitable[None]]
# 参数：(symbol, bid, ask, ts_ms)


class BinanceWSFeed:
    def __init__(self, on_price: PriceCallback):
        self.on_price = on_price
        self.subscribed: set[str] = set()
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._stop = False
        self._req_id = 0

    def _next_id(self) -> int:
        self._req_id += 1
        return self._req_id

    async def start(self):
        backoff = 1
        while not self._stop:
            try:
                # !bookTicker 是所有symbol的订阅，但流量大；我们用单独订阅list
                async with websockets.connect(
                    f"{BINANCE_FUTURES_WS}?streams=",
                    ping_interval=180,    # 币安要求<10分钟发一次ping
                    ping_timeout=20,
                    max_size=2**20,
                ) as ws:
                    self.ws = ws
                    backoff = 1
                    # 重连后重新订阅
                    if self.subscribed:
                        await self._send_subscribe(list(self.subscribed))

                    async for raw in ws:
                        await self._handle_message(raw)
            except Exception as e:
                await DB.log_event("warn", f"Binance WS error: {e}; reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self):
        self._stop = True
        if self.ws:
            await self.ws.close()

    async def _handle_message(self, raw: str):
        try:
            msg = json.loads(raw)
        except Exception:
            return
        if "data" not in msg:
            return  # 订阅确认等
        d = msg["data"]
        if d.get("e") != "bookTicker" and "s" not in d:
            return
        symbol = d.get("s")
        try:
            bid = float(d.get("b", 0))
            ask = float(d.get("a", 0))
        except Exception:
            return
        ts = int(d.get("E", int(time.time() * 1000)))
        if bid > 0 and ask > 0 and symbol:
            await self.on_price(symbol, bid, ask, ts)

    async def _send_subscribe(self, symbols: list[str]):
        if not self.ws or not symbols:
            return
        params = [f"{s.lower()}@bookTicker" for s in symbols]
        msg = {"method": "SUBSCRIBE", "params": params, "id": self._next_id()}
        await self.ws.send(json.dumps(msg))

    async def _send_unsubscribe(self, symbols: list[str]):
        if not self.ws or not symbols:
            return
        params = [f"{s.lower()}@bookTicker" for s in symbols]
        msg = {"method": "UNSUBSCRIBE", "params": params, "id": self._next_id()}
        await self.ws.send(json.dumps(msg))

    async def update_subscriptions(self, symbols: set[str]):
        """对比当前订阅列表，增量订阅/退订"""
        to_add = symbols - self.subscribed
        to_remove = self.subscribed - symbols

        if to_add:
            await self._send_subscribe(list(to_add))
            self.subscribed |= to_add
        if to_remove:
            await self._send_unsubscribe(list(to_remove))
            self.subscribed -= to_remove

        if to_add or to_remove:
            await DB.log_event(
                "info",
                f"CEX feed: +{len(to_add)} -{len(to_remove)} (total={len(self.subscribed)})",
            )
