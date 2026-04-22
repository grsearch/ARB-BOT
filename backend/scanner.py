"""
标的扫描器 v3 - 纯链上权威：

Step 1: 币安合约 Top N 涨幅榜（只拿 USDT 本位永续）
Step 2: 对每个 symbol 直接在 BSC 链上搜池子：
  - 先扫 V3 Factory 的 4 档 fee tier（token/USDT, token/WBNB）
  - 再扫 V2 Factory 的 1 个 pair（token/USDT, token/WBNB）
  - 优先 V3（更省 gas、可选 fee 档），V2 作为回退
  - 用链上流动性数据估算 TVL（USDT*2 或 WBNB*2*BNB价）
Step 3: 过滤 TVL < min_pool_tvl_usd 的池子

取消了币安官方"BSC充提白名单"过滤 —— 因为那是账户级数据不稳定。
现在的原则：能在 BSC 找到真实流动池 = 可套利。

关键：token 地址从哪来？
  - 方案A：币安 capital API 的 contractAddress 字段（有时有有时没有）
  - 方案B：GeckoTerminal search（补充）
  - 方案C：MANUAL_OVERRIDE 手填
  以上三路数据源合并去重。
"""
import asyncio
import aiohttp
import time
import hmac
import hashlib
from urllib.parse import urlencode
from typing import Optional
from web3 import Web3
from .config import (
    STATIC, RUNTIME, RuntimeConfig,
    BINANCE_SPOT_REST, BINANCE_FUTURES_REST, GECKO_TERMINAL_REST,
    PANCAKE_V3_FACTORY, PANCAKE_V2_FACTORY, V3_FEE_TIERS, V2_SWAP_FEE_BPS,
    USDT, WBNB,
)
from .abi import V3_FACTORY_ABI, V3_POOL_ABI, V2_FACTORY_ABI, V2_PAIR_ABI, ERC20_ABI
from .db import DB


# 人工地址覆盖（最高优先级）。如果 GeckoTerminal 和币安都找不到，可以在这里手填
MANUAL_OVERRIDE = {
    # "CAKE": "0x0e09fabb73bd3ade0a17ecc321fd13a19e81ce82",
    # "C":    "0x...",
    # "HOLO": "0x...",
}


class Scanner:
    def __init__(self, w3: Web3):
        self.w3 = w3
        self.v3_factory = w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_FACTORY),
            abi=V3_FACTORY_ABI,
        )
        self.v2_factory = w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V2_FACTORY),
            abi=V2_FACTORY_ABI,
        )
        self._session: Optional[aiohttp.ClientSession] = None

        # 缓存：symbol -> token_address（避免反复查）
        self._token_cache: dict[str, str] = {}
        # 缓存：pool 元数据
        self._pool_cache: dict[str, dict] = {}
        # 币安 capital list（仅用于补充合约地址，不再作过滤门）
        self._binance_capital: dict[str, str] = {}  # base_asset -> token_address
        self._capital_ts: float = 0
        # BNB/USD 参考价引用（由 engine 维护）
        self._bnb_price_ref: Optional[dict] = None

    def bind_bnb_ref(self, ref: dict):
        self._bnb_price_ref = ref

    async def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15),
                headers={"User-Agent": "arb-bot/1.0"},
            )
        return self._session

    async def close(self):
        if self._session and not self._session.closed:
            await self._session.close()

    # =====================================================================
    # 步骤1：币安 capital 信息（只当作地址来源，不再做过滤）
    # =====================================================================
    async def refresh_binance_capital(self):
        """每天刷一次，拿币安 capital/config 里的 BSC contractAddress 作为地址候选。"""
        now = time.time()
        if self._binance_capital and now - self._capital_ts < 86400:
            return

        session = await self._get_session()
        endpoint = "/sapi/v1/capital/config/getall"
        ts = int(time.time() * 1000)
        params = {"timestamp": ts, "recvWindow": 5000}
        query = urlencode(params)
        signature = hmac.new(
            STATIC.binance_api_secret.encode(), query.encode(), hashlib.sha256
        ).hexdigest()
        url = f"{BINANCE_SPOT_REST}{endpoint}?{query}&signature={signature}"
        headers = {"X-MBX-APIKEY": STATIC.binance_api_key}

        try:
            async with session.get(url, headers=headers) as r:
                if r.status != 200:
                    text = await r.text()
                    await DB.log_event("warn", f"capital/config HTTP {r.status}: {text[:200]}")
                    return
                data = await r.json()
        except Exception as e:
            await DB.log_event("warn", f"capital/config err: {e}")
            return

        result: dict[str, str] = {}
        for c in data:
            coin = c.get("coin", "")
            for n in c.get("networkList", []):
                if n.get("network") == "BSC":
                    addr = (n.get("contractAddress") or "").strip().lower()
                    if addr.startswith("0x") and len(addr) == 42:
                        try:
                            result[coin] = Web3.to_checksum_address(addr)
                        except Exception:
                            pass
                    break
        self._binance_capital = result
        self._capital_ts = now
        await DB.log_event("info", f"Binance capital: got {len(result)} BSC contract addrs (reference only)")

    # =====================================================================
    # 步骤2：涨幅榜
    # =====================================================================
    async def fetch_top_gainers(self, top_n: int, min_gain: float) -> list[dict]:
        session = await self._get_session()
        url = f"{BINANCE_FUTURES_REST}/fapi/v1/ticker/24hr"
        try:
            async with session.get(url) as r:
                data = await r.json()
        except Exception as e:
            await DB.log_event("error", f"top_gainers fetch err: {e}")
            return []

        perps = [
            x for x in data
            if x["symbol"].endswith("USDT")
            and not any(s in x["symbol"] for s in ("UP", "DOWN", "BULL", "BEAR", "_"))
        ]
        perps.sort(key=lambda x: float(x["priceChangePercent"]), reverse=True)

        results = []
        for p in perps:
            gain = float(p["priceChangePercent"]) / 100.0
            if gain < min_gain:
                break
            results.append({
                "symbol": p["symbol"],
                "base_asset": p["symbol"].replace("USDT", ""),
                "change_24h_pct": gain,
                "last_cex_price": float(p["lastPrice"]),
                "volume_usdt": float(p["quoteVolume"]),
            })
            if len(results) >= top_n:
                break
        return results

    # =====================================================================
    # 步骤3：获取 token 地址（3 路合并）
    # =====================================================================
    async def get_token_address(self, base_asset: str) -> Optional[str]:
        if base_asset in self._token_cache:
            return self._token_cache[base_asset]

        # 1) 人工覆盖
        if base_asset in MANUAL_OVERRIDE:
            try:
                addr = Web3.to_checksum_address(MANUAL_OVERRIDE[base_asset])
                self._token_cache[base_asset] = addr
                return addr
            except Exception:
                pass

        # 2) 币安 capital
        if base_asset in self._binance_capital:
            addr = self._binance_capital[base_asset]
            self._token_cache[base_asset] = addr
            return addr

        # 3) GeckoTerminal search
        addr = await self._gecko_search_token(base_asset)
        if addr:
            self._token_cache[base_asset] = addr
            return addr

        return None

    async def _gecko_search_token(self, base_asset: str) -> Optional[str]:
        """从 GeckoTerminal 搜 BSC 上 symbol 匹配的 token，取流动性最大的。"""
        session = await self._get_session()
        url = f"{GECKO_TERMINAL_REST}/search/pools"
        params = {"query": base_asset, "network": "bsc", "page": 1}
        try:
            async with session.get(url, params=params) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        except Exception:
            return None
        pools = (data or {}).get("data", [])
        if not pools:
            return None

        # 找第一个 base symbol 匹配的池子
        for p in pools:
            attr = p.get("attributes", {}) or {}
            rels = p.get("relationships", {}) or {}
            name = attr.get("name", "")
            base_part = name.split("/")[0].strip().upper() if "/" in name else ""
            if base_part != base_asset.upper():
                continue
            base_id = (rels.get("base_token", {}).get("data", {}) or {}).get("id", "")
            for prefix in ("bsc_", "bnb_", "bscmainnet_"):
                if base_id.startswith(prefix):
                    base_id = base_id[len(prefix):]
                    break
            if base_id.startswith("0x") and len(base_id) == 42:
                try:
                    return Web3.to_checksum_address(base_id)
                except Exception:
                    continue
        return None

    # =====================================================================
    # 步骤4：链上找最佳池子（V3 优先，V2 回退）
    # =====================================================================
    async def find_best_pool(self, token_addr: str, min_tvl_usd: float, max_fee_bps: int) -> Optional[dict]:
        """
        返回最佳池子信息：
        {
          version: 'v3' | 'v2',
          pool_address, pool_fee_bps, pool_fee_pct(百分数 0.25),
          token_decimals, pool_tvl_usd,
          quote_token: USDT or WBNB,
        }
        """
        token_cs = Web3.to_checksum_address(token_addr)
        try:
            token_dec = await asyncio.to_thread(
                self.w3.eth.contract(address=token_cs, abi=ERC20_ABI).functions.decimals().call
            )
        except Exception as e:
            await DB.log_event("warn", f"decimals read fail {token_cs}: {e}")
            return None

        candidates: list[dict] = []

        # -------- V3 扫描 (4 fee tiers × 2 quotes) --------
        for quote_addr, quote_sym in ((USDT, "USDT"), (WBNB, "WBNB")):
            quote_cs = Web3.to_checksum_address(quote_addr)
            quote_dec = 18   # BSC USDT/WBNB 都是18位
            for fee in V3_FEE_TIERS:
                if fee > max_fee_bps:
                    continue
                try:
                    pool = await asyncio.to_thread(
                        self.v3_factory.functions.getPool(token_cs, quote_cs, fee).call
                    )
                except Exception:
                    continue
                if not pool or int(pool, 16) == 0:
                    continue
                tvl = await self._v3_pool_tvl(pool, token_cs, quote_cs, token_dec, quote_dec, quote_sym)
                if tvl is None or tvl < min_tvl_usd:
                    continue
                candidates.append({
                    "version": "v3",
                    "pool_address": Web3.to_checksum_address(pool),
                    "pool_fee_bps": fee,
                    "pool_fee_pct": fee / 10000.0,
                    "token_decimals": token_dec,
                    "pool_tvl_usd": tvl,
                    "quote_token": quote_cs,
                    "quote_symbol": quote_sym,
                })

        # -------- V2 扫描（回退） --------
        for quote_addr, quote_sym in ((USDT, "USDT"), (WBNB, "WBNB")):
            quote_cs = Web3.to_checksum_address(quote_addr)
            try:
                pair = await asyncio.to_thread(
                    self.v2_factory.functions.getPair(token_cs, quote_cs).call
                )
            except Exception:
                continue
            if not pair or int(pair, 16) == 0:
                continue
            tvl = await self._v2_pool_tvl(pair, token_cs, quote_cs, token_dec, 18, quote_sym)
            if tvl is None or tvl < min_tvl_usd:
                continue
            # V2 固定 0.25%。V3 的 fee 单位是 "per 1,000,000"（2500=0.25%）
            # V2_SWAP_FEE_BPS=25 代表 25/10000 = 0.25%，换算到 V3 单位就是 25 * 100 = 2500
            v2_fee_in_v3_units = V2_SWAP_FEE_BPS * 100
            if v2_fee_in_v3_units > max_fee_bps:
                continue
            candidates.append({
                "version": "v2",
                "pool_address": Web3.to_checksum_address(pair),
                "pool_fee_bps": v2_fee_in_v3_units,
                "pool_fee_pct": 0.25,
                "token_decimals": token_dec,
                "pool_tvl_usd": tvl,
                "quote_token": quote_cs,
                "quote_symbol": quote_sym,
            })

        if not candidates:
            return None

        # 优先 USDT 报价（少一跳），其次 TVL 最大
        usdt_pools = [c for c in candidates if c["quote_token"].lower() == USDT.lower()]
        pool_set = usdt_pools if usdt_pools else candidates
        best = max(pool_set, key=lambda x: x["pool_tvl_usd"])
        return best

    async def _v3_pool_tvl(self, pool, token, quote, token_dec, quote_dec, quote_sym) -> Optional[float]:
        """V3 TVL 估算：用池子当前持有的两个token余额各乘以价格求和。"""
        try:
            token_ct = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            quote_ct = self.w3.eth.contract(address=Web3.to_checksum_address(quote), abi=ERC20_ABI)
            bal_token = await asyncio.to_thread(token_ct.functions.balanceOf(Web3.to_checksum_address(pool)).call)
            bal_quote = await asyncio.to_thread(quote_ct.functions.balanceOf(Web3.to_checksum_address(pool)).call)
        except Exception:
            return None
        quote_amount_usd = self._quote_to_usd(bal_quote, quote_dec, quote_sym)
        if quote_amount_usd is None:
            return None
        # TVL ≈ 2 × quote_amount_usd （假定两侧价值平衡，V3 集中流动性不完全平衡但近似可用）
        return quote_amount_usd * 2

    async def _v2_pool_tvl(self, pair, token, quote, token_dec, quote_dec, quote_sym) -> Optional[float]:
        """V2 TVL：直接 getReserves() 算。"""
        try:
            pc = self.w3.eth.contract(address=Web3.to_checksum_address(pair), abi=V2_PAIR_ABI)
            r0, r1, _ = await asyncio.to_thread(pc.functions.getReserves().call)
            t0 = await asyncio.to_thread(pc.functions.token0().call)
        except Exception:
            return None
        # 判断 quote 是 token0 还是 token1
        if t0.lower() == quote.lower():
            reserve_quote = r0
        else:
            reserve_quote = r1
        quote_usd = self._quote_to_usd(reserve_quote, quote_dec, quote_sym)
        if quote_usd is None:
            return None
        return quote_usd * 2

    def _quote_to_usd(self, wei_amount: int, decimals: int, symbol: str) -> Optional[float]:
        amount = wei_amount / (10 ** decimals)
        if symbol == "USDT":
            return amount
        if symbol == "WBNB":
            bnb_usd = (self._bnb_price_ref or {}).get("price", 0) if self._bnb_price_ref else 0
            if not bnb_usd:
                bnb_usd = 600   # fallback，TVL估算用；实际PnL会用真实BNB价
            return amount * bnb_usd
        return None

    # =====================================================================
    # 顶层：完整扫描
    # =====================================================================
    async def run_once(self, rt: RuntimeConfig) -> list[dict]:
        await DB.log_event("info", "--- Scan start ---")
        await self.refresh_binance_capital()

        gainers = await self.fetch_top_gainers(rt.top_n_gainers, rt.min_24h_gain_pct)
        await DB.log_event("info", f"Top gainers (>= {rt.min_24h_gain_pct*100:.1f}%): {len(gainers)}")

        confirmed: list[dict] = []
        skipped = []
        for g in gainers:
            base = g["base_asset"]
            try:
                token_addr = await self.get_token_address(base)
                if not token_addr:
                    skipped.append(f"{base}(no_addr)")
                    continue

                pool = await self.find_best_pool(token_addr, rt.min_pool_tvl_usd, rt.max_pool_fee_bps)
                if not pool:
                    skipped.append(f"{base}(no_pool/tvl_low)")
                    continue

                cand = {
                    "symbol": g["symbol"],
                    "base_asset": base,
                    "token_address": token_addr,
                    "pool_address": pool["pool_address"],
                    "pool_fee": pool["pool_fee_bps"],
                    "pool_fee_pct": pool["pool_fee_pct"],
                    "pool_tvl_usd": pool["pool_tvl_usd"],
                    "pool_24h_vol_usd": None,
                    "decimals": pool["token_decimals"],
                    "change_24h_pct": g["change_24h_pct"],
                    "last_cex_price": g["last_cex_price"],
                    "last_dex_price": None,
                    "last_basis_pct": None,
                    "pool_version": pool["version"],
                    "quote_token": pool["quote_token"],
                    "source": "onchain",
                }
                await DB.upsert_candidate(cand)
                confirmed.append(cand)
                await DB.log_event(
                    "info",
                    f"OK {g['symbol']} [{pool['version'].upper()}] "
                    f"TVL=${pool['pool_tvl_usd']:,.0f} fee={pool['pool_fee_pct']:.2f}% "
                    f"quote={pool['quote_symbol']} pool={pool['pool_address'][:10]}..."
                )
            except Exception as e:
                skipped.append(f"{base}(err:{e})")
                await DB.log_event("error", f"Scan err {base}: {e}")

        if skipped:
            await DB.log_event("info", f"Skipped: {', '.join(skipped[:20])}")

        # 空扫描不覆盖
        if confirmed:
            kept = {c["symbol"] for c in confirmed}
            existing = await DB.fetchall("SELECT symbol FROM candidates", ())
            for row in existing:
                if row["symbol"] not in kept:
                    await DB.execute("DELETE FROM candidates WHERE symbol=?", (row["symbol"],))
            await DB.log_event("info", f"--- Scan done: {len(confirmed)} candidates ---")
        else:
            await DB.log_event("warn", "--- Scan done: 0 candidates, keeping previous ---")
        return confirmed


async def run_scanner_loop(w3: Web3, on_update, bnb_price_ref: dict):
    scanner = Scanner(w3)
    scanner.bind_bnb_ref(bnb_price_ref)
    try:
        while True:
            if RUNTIME.enabled:
                try:
                    cands = await scanner.run_once(RUNTIME)
                    await on_update(cands)
                except Exception as e:
                    await DB.log_event("error", f"Scanner loop err: {e}")
            await asyncio.sleep(RUNTIME.scan_interval_sec)
    finally:
        await scanner.close()
