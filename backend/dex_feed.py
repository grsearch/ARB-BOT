"""
DEX 价格流（双通道）：
1. 主通道：Birdeye WebSocket（Premium Plus, 500并发连接），延迟~100ms
2. 备通道：链上 slot0() 直读 + multicall 批量（200-400ms）
    当 Birdeye 超过 3 秒无更新时自动切到链上查询。

我们对每个 symbol 维护 last_price + last_update_ts，engine 消费时取较新者。
"""
import asyncio
import json
import time
import websockets
from typing import Callable, Awaitable, Optional
from web3 import Web3
from .config import STATIC, BIRDEYE_WS_BSC, USDT, WBNB
from .abi import V3_POOL_ABI, ERC20_ABI
from .db import DB


PriceCallback = Callable[[str, float, int, str], Awaitable[None]]
# 参数：(symbol, price_usd, ts_ms, source)


class BirdeyeWSFeed:
    """
    Birdeye WS 协议：
      订阅: {"type":"SUBSCRIBE_PRICE","data":{"queryType":"simple",
              "chartType":"1m","address":"<token>","currency":"usd"}}
      推送: {"type":"PRICE_DATA","data":{"o":..,"h":..,"l":..,"c":..,"address":..}}
    注意：1m是最小粒度（BSC链上），每分钟会推多次（非按分钟固定）。
    """

    def __init__(self, on_price: PriceCallback):
        self.on_price = on_price
        self.ws: Optional[websockets.WebSocketClientProtocol] = None
        self._stop = False
        self.subscribed: dict[str, str] = {}   # token_address(lower) -> symbol
        self._send_lock = asyncio.Lock()

    async def start(self):
        backoff = 1
        # websockets 12+ 用 additional_headers, 老版本用 extra_headers
        try:
            import inspect
            sig = inspect.signature(websockets.connect)
            header_kw = "additional_headers" if "additional_headers" in sig.parameters else "extra_headers"
        except Exception:
            header_kw = "additional_headers"

        while not self._stop:
            try:
                uri = f"{BIRDEYE_WS_BSC}?x-api-key={STATIC.birdeye_api_key}"
                kwargs = {
                    "subprotocols": ["echo-protocol"],
                    "ping_interval": 30,
                    "ping_timeout": 20,
                    "max_size": 2**20,
                    header_kw: {
                        "Origin": "ws://public-api.birdeye.so",
                        "Sec-WebSocket-Origin": "ws://public-api.birdeye.so",
                    },
                }
                async with websockets.connect(uri, **kwargs) as ws:
                    self.ws = ws
                    backoff = 1
                    # 重连后重订
                    for addr in list(self.subscribed.keys()):
                        await self._send_subscribe(addr)
                    async for raw in ws:
                        await self._handle(raw)
            except Exception as e:
                await DB.log_event("warn", f"Birdeye WS error: {e}; reconnect in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def stop(self):
        self._stop = True
        if self.ws:
            await self.ws.close()

    async def _handle(self, raw: str):
        try:
            m = json.loads(raw)
        except Exception:
            return
        if m.get("type") != "PRICE_DATA":
            return
        d = m.get("data") or {}
        addr = (d.get("address") or "").lower()
        price = d.get("c")   # close price of latest candle
        if not addr or price is None:
            return
        try:
            price = float(price)
        except Exception:
            return
        sym = self.subscribed.get(addr)
        if not sym:
            return
        ts = int(time.time() * 1000)
        await self.on_price(sym, price, ts, "birdeye")

    async def _send_subscribe(self, token_address_lower: str):
        if not self.ws:
            return
        msg = {
            "type": "SUBSCRIBE_PRICE",
            "data": {
                "queryType": "simple",
                "chartType": "1m",
                "address": Web3.to_checksum_address(token_address_lower),
                "currency": "usd",
            },
        }
        async with self._send_lock:
            await self.ws.send(json.dumps(msg))
            await asyncio.sleep(0.05)   # 防止突发订阅被拒

    async def _send_unsubscribe(self, token_address_lower: str):
        if not self.ws:
            return
        msg = {
            "type": "UNSUBSCRIBE_PRICE",
            "data": {"address": Web3.to_checksum_address(token_address_lower)},
        }
        async with self._send_lock:
            await self.ws.send(json.dumps(msg))

    async def update_subscriptions(self, targets: dict[str, str]):
        """targets: {token_address_lower: symbol}"""
        new_set = set(targets.keys())
        old_set = set(self.subscribed.keys())

        to_add = new_set - old_set
        to_remove = old_set - new_set

        for addr in to_add:
            self.subscribed[addr] = targets[addr]
            await self._send_subscribe(addr)
        for addr in to_remove:
            self.subscribed.pop(addr, None)
            await self._send_unsubscribe(addr)

        if to_add or to_remove:
            await DB.log_event(
                "info",
                f"DEX feed: +{len(to_add)} -{len(to_remove)} (total={len(self.subscribed)})",
            )


class OnChainPriceReader:
    """
    链上价格直读兜底。
    通过 V3 pool 的 slot0() 获得 sqrtPriceX96，换算出价格。

    对一个 pool (token/quote)：
      price_of_token0_in_token1 = (sqrtPriceX96 / 2^96)^2
    需要根据 token0/token1 顺序和 decimals 归一化。
    对 quote = USDT（6位或18位）直接出USD价；
    对 quote = WBNB 需要乘以 BNB/USD 价（我们从币安拿）。
    """

    def __init__(self, w3: Web3, bnb_price_ref: dict):
        self.w3 = w3
        self.bnb_price_ref = bnb_price_ref   # {'price': float}
        self.pool_meta: dict[str, dict] = {}  # pool_addr -> {token0, token1, t0_dec, t1_dec, base_is_token0, quote}

    async def prime_pool(self, pool_addr: str, base_token: str, base_dec: int):
        pool_cs = Web3.to_checksum_address(pool_addr)
        # 已缓存就跳过
        if pool_cs.lower() in self.pool_meta:
            return
        pc = self.w3.eth.contract(address=pool_cs, abi=V3_POOL_ABI)
        token0 = await asyncio.to_thread(pc.functions.token0().call)
        token1 = await asyncio.to_thread(pc.functions.token1().call)

        # quote的decimals
        def _get_dec(addr: str) -> int:
            ec = self.w3.eth.contract(address=Web3.to_checksum_address(addr), abi=ERC20_ABI)
            return ec.functions.decimals().call()

        t0_dec = await asyncio.to_thread(_get_dec, token0)
        t1_dec = await asyncio.to_thread(_get_dec, token1)
        base_is_token0 = token0.lower() == base_token.lower()
        quote = token1 if base_is_token0 else token0
        self.pool_meta[pool_cs.lower()] = {
            "token0": token0, "token1": token1,
            "t0_dec": t0_dec, "t1_dec": t1_dec,
            "base_is_token0": base_is_token0,
            "quote": quote.lower(),
        }

    async def read_price(self, pool_addr: str) -> Optional[float]:
        meta = self.pool_meta.get(pool_addr.lower())
        if not meta:
            return None
        pc = self.w3.eth.contract(
            address=Web3.to_checksum_address(pool_addr), abi=V3_POOL_ABI
        )
        try:
            slot0 = await asyncio.to_thread(pc.functions.slot0().call)
        except Exception:
            return None
        sqrtPriceX96 = slot0[0]
        if sqrtPriceX96 <= 0:
            return None

        # Uniswap V3 定义:
        #   sqrtPriceX96 = sqrt(price) * 2^96
        #   price = (sqrtPriceX96 / 2^96)^2  == amount of token1 per 1 unit of token0 (both in wei/smallest unit)
        # 转成人类可读价格（1 token0 = ? token1）:
        #   price_human = price_raw * 10^(t0_dec - t1_dec)
        price_raw_01 = (sqrtPriceX96 / (2 ** 96)) ** 2
        price_human_01 = price_raw_01 * (10 ** (meta["t0_dec"] - meta["t1_dec"]))
        # 即：1 token0 = price_human_01 token1

        if meta["base_is_token0"]:
            base_in_quote = price_human_01
        else:
            base_in_quote = 1 / price_human_01 if price_human_01 else 0

        # 换算为USD
        quote_addr = meta["quote"]
        if quote_addr == USDT.lower():
            return base_in_quote
        if quote_addr == WBNB.lower():
            bnb_usd = self.bnb_price_ref.get("price", 0)
            return base_in_quote * bnb_usd if bnb_usd else None
        return None


class DexFeedManager:
    """
    汇总 Birdeye + 链上读取。
    - 收到 Birdeye 推送 -> 更新缓存
    - 每 1s 检查：若某symbol >3s无更新，则主动 on-chain 读一次
    """

    def __init__(self, w3: Web3, on_price: PriceCallback, bnb_price_ref: dict):
        self.w3 = w3
        self.on_price = on_price
        self.prices: dict[str, dict] = {}  # symbol -> {price, ts, source}
        self.birdeye = BirdeyeWSFeed(self._on_birdeye_price)
        self.onchain = OnChainPriceReader(w3, bnb_price_ref)
        self.meta: dict[str, dict] = {}    # symbol -> {token, pool, dec}

    async def _on_birdeye_price(self, symbol: str, price: float, ts: int, source: str):
        self.prices[symbol] = {"price": price, "ts": ts, "source": source}
        await self.on_price(symbol, price, ts, source)

    async def start(self):
        asyncio.create_task(self.birdeye.start())
        asyncio.create_task(self._onchain_loop())

    async def update_candidates(self, candidates: list[dict]):
        targets: dict[str, str] = {}
        for c in candidates:
            if not c.get("token_address") or not c.get("pool_address"):
                continue
            sym = c["symbol"]
            addr = c["token_address"].lower()
            targets[addr] = sym
            self.meta[sym] = {
                "token": c["token_address"],
                "pool": c["pool_address"],
                "decimals": c.get("decimals", 18),
            }
            # 预填pool meta
            try:
                await self.onchain.prime_pool(c["pool_address"], c["token_address"], c.get("decimals", 18))
            except Exception as e:
                await DB.log_event("warn", f"onchain prime failed {sym}: {e}")

        await self.birdeye.update_subscriptions(targets)

        # 清理不再监控的symbol
        for sym in list(self.prices.keys()):
            if sym not in {c["symbol"] for c in candidates}:
                self.prices.pop(sym, None)
                self.meta.pop(sym, None)

    async def _onchain_loop(self):
        """每秒检查，补齐过期价格"""
        while True:
            try:
                now = int(time.time() * 1000)
                for sym, meta in list(self.meta.items()):
                    last = self.prices.get(sym)
                    if last and now - last["ts"] < 3000:
                        continue  # Birdeye还新鲜
                    pool = meta.get("pool")
                    if not pool:
                        continue
                    p = await self.onchain.read_price(pool)
                    if p and p > 0:
                        self.prices[sym] = {"price": p, "ts": now, "source": "onchain"}
                        await self.on_price(sym, p, now, "onchain")
            except Exception as e:
                await DB.log_event("warn", f"onchain loop err: {e}")
            await asyncio.sleep(1.0)
