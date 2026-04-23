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

        # -------- Infinity 扫描（仅监控，不交易） --------
        # Infinity 池子发现用 GeckoTerminal（链上发现需订阅 Initialize 事件，下版本再做）
        try:
            inf_pools = await self._find_infinity_pools(token_cs, token_dec)
            candidates.extend(inf_pools)
        except Exception as e:
            await DB.log_event("warn", f"infinity scan err: {e}")

        if not candidates:
            return None

        # 优先 USDT 报价（少一跳），其次 TVL 最大
        # 但 Infinity 池子暂时不自动选为"最佳"（因为不能交易），
        # 除非它是唯一的池子
        tradable = [c for c in candidates if c["version"] in ("v2", "v3")]
        if tradable:
            usdt_pools = [c for c in tradable if c["quote_token"].lower() == USDT.lower()]
            pool_set = usdt_pools if usdt_pools else tradable
            best = max(pool_set, key=lambda x: x["pool_tvl_usd"])
        else:
            # 只有 Infinity 池子可用 → 记录但标记 tradable=False
            best = max(candidates, key=lambda x: x["pool_tvl_usd"])
        return best

    async def _v3_pool_tvl(self, pool, token, quote, token_dec, quote_dec, quote_sym) -> Optional[float]:
        """
        V3 TVL 估算（从链上池子两侧实际余额计算）：
          TVL = balance_token * token_price_usd + balance_quote * quote_price_usd

        对新池子，两侧余额可能极度不平衡（比如只有一侧），必须两边都算。
        token_price 从池子自己的 slot0 推出（当前价格）。
        """
        try:
            pool_cs = Web3.to_checksum_address(pool)
            token_ct = self.w3.eth.contract(address=Web3.to_checksum_address(token), abi=ERC20_ABI)
            quote_ct = self.w3.eth.contract(address=Web3.to_checksum_address(quote), abi=ERC20_ABI)
            bal_token = await asyncio.to_thread(token_ct.functions.balanceOf(pool_cs).call)
            bal_quote = await asyncio.to_thread(quote_ct.functions.balanceOf(pool_cs).call)
        except Exception:
            return None

        # quote 侧换成USD
        quote_usd = self._quote_to_usd(bal_quote, quote_dec, quote_sym) or 0

        # token 侧：需要 token 的 USD 价格。从池子 slot0 推导
        token_usd_price = await self._token_price_from_v3_pool(pool, token, quote, token_dec, quote_dec, quote_sym)
        token_side_usd = 0
        if token_usd_price and bal_token > 0:
            token_amount = bal_token / (10 ** token_dec)
            token_side_usd = token_amount * token_usd_price

        tvl = quote_usd + token_side_usd
        return tvl if tvl > 0 else None

    async def _token_price_from_v3_pool(self, pool, token, quote, token_dec, quote_dec, quote_sym) -> Optional[float]:
        """通过 V3 slot0 算出 token 的 USD 价格"""
        try:
            pc = self.w3.eth.contract(address=Web3.to_checksum_address(pool), abi=V3_POOL_ABI)
            slot0 = await asyncio.to_thread(pc.functions.slot0().call)
            t0 = await asyncio.to_thread(pc.functions.token0().call)
        except Exception:
            return None
        sqrtPriceX96 = slot0[0]
        if sqrtPriceX96 <= 0:
            return None

        # price_human_01 = 1 token0 等于多少 token1 (人类单位)
        token0_is_token = (t0.lower() == token.lower())
        if token0_is_token:
            t0_dec, t1_dec = token_dec, quote_dec
        else:
            t0_dec, t1_dec = quote_dec, token_dec

        price_raw = (sqrtPriceX96 / (2**96)) ** 2
        price_human_01 = price_raw * (10 ** (t0_dec - t1_dec))

        # 目标：token 的价格 (以 quote 计量)
        if token0_is_token:
            token_in_quote = price_human_01
        else:
            token_in_quote = 1 / price_human_01 if price_human_01 > 0 else 0

        # 把 quote 折算到 USD
        if quote_sym == "USDT":
            return token_in_quote
        if quote_sym == "WBNB":
            bnb_usd = (self._bnb_price_ref or {}).get("price", 0) if self._bnb_price_ref else 0
            if not bnb_usd:
                bnb_usd = 600
            return token_in_quote * bnb_usd
        return None

    async def _find_infinity_pools(self, token_cs: str, token_dec: int) -> list[dict]:
        """
        通过 GeckoTerminal 发现 Infinity (CLMM/LBAMM) 池子。
        仅用于监控展示 —— 当前版本 Dashboard 显示基差但不自动交易。
        完整 Universal Router 集成在下一版本实现。

        GeckoTerminal dex_id 格式：
          - pancakeswap-v2-bsc
          - pancakeswap-v3-bsc
          - pancakeswap-infinity-clmm-bsc   ← Infinity CLAMM
          - pancakeswap-infinity-lbamm-bsc  ← Infinity LBAMM
        """
        session = await self._get_session()
        url = f"{GECKO_TERMINAL_REST}/networks/bsc/tokens/{token_cs.lower()}/pools"
        params = {"page": 1}
        try:
            async with session.get(url, params=params) as r:
                if r.status != 200:
                    return []
                data = await r.json()
        except Exception:
            return []

        pools_raw = (data or {}).get("data", [])
        found: list[dict] = []
        for p in pools_raw:
            attr = p.get("attributes", {}) or {}
            rels = p.get("relationships", {}) or {}
            dex_id = (rels.get("dex", {}).get("data", {}) or {}).get("id", "").lower()
            if "infinity" not in dex_id:
                continue

            # Infinity 类型
            if "clmm" in dex_id:
                inf_type = "infinity-clmm"
            elif "lbamm" in dex_id:
                inf_type = "infinity-lbamm"
            else:
                inf_type = "infinity"

            pool_addr_raw = (attr.get("address") or "").strip()
            if not pool_addr_raw.startswith("0x") or len(pool_addr_raw) != 42:
                continue
            try:
                pool_addr = Web3.to_checksum_address(pool_addr_raw)
            except Exception:
                continue

            # 从 GeckoTerminal 读 TVL 和 quote token
            tvl = 0.0
            try:
                tvl = float(attr.get("reserve_in_usd") or 0)
            except Exception:
                pass

            # quote token 判断
            base_id = (rels.get("base_token", {}).get("data", {}) or {}).get("id", "")
            quote_id = (rels.get("quote_token", {}).get("data", {}) or {}).get("id", "")
            # 把 "bsc_0x..." 截断成地址
            def _strip(x):
                for pfx in ("bsc_", "bnb_", "bscmainnet_"):
                    if x.startswith(pfx):
                        return x[len(pfx):]
                return x
            base_addr = _strip(base_id).lower()
            quote_addr_raw = _strip(quote_id).lower()
            # 判断 base/quote 中哪个是我们的 token
            if base_addr == token_cs.lower():
                other = quote_addr_raw
            elif quote_addr_raw == token_cs.lower():
                other = base_addr
            else:
                # GeckoTerminal 返回的 token 不匹配，跳过
                continue

            if other == USDT.lower():
                quote_sym, quote_cs = "USDT", Web3.to_checksum_address(USDT)
            elif other == WBNB.lower():
                quote_sym, quote_cs = "WBNB", Web3.to_checksum_address(WBNB)
            else:
                # 非 USDT/WBNB 计价对暂不支持
                continue

            # Infinity 池的 fee 也可能是动态的，GeckoTerminal 只给粗略值
            pool_fee_pct_str = attr.get("fee_tier") or attr.get("pool_fee")
            pool_fee_pct = 0.25  # 默认
            pool_fee_bps = 2500
            if pool_fee_pct_str:
                try:
                    # 可能是 "0.25%" 或 "0.003"
                    s = str(pool_fee_pct_str).replace("%", "").strip()
                    v = float(s)
                    if v > 1:       # "0.25"
                        pool_fee_pct = v
                    else:            # "0.003" = 0.3%
                        pool_fee_pct = v * 100
                    pool_fee_bps = int(round(pool_fee_pct * 10000))
                except Exception:
                    pass

            found.append({
                "version": inf_type,              # 'infinity-clmm' 或 'infinity-lbamm'
                "pool_address": pool_addr,
                "pool_fee_bps": pool_fee_bps,
                "pool_fee_pct": pool_fee_pct,
                "token_decimals": token_dec,
                "pool_tvl_usd": tvl,
                "quote_token": quote_cs,
                "quote_symbol": quote_sym,
            })

        return found

    async def _v2_pool_tvl(self, pair, token, quote, token_dec, quote_dec, quote_sym) -> Optional[float]:
        """V2 TVL：getReserves()，两边都算。"""
        try:
            pc = self.w3.eth.contract(address=Web3.to_checksum_address(pair), abi=V2_PAIR_ABI)
            r0, r1, _ = await asyncio.to_thread(pc.functions.getReserves().call)
            t0 = await asyncio.to_thread(pc.functions.token0().call)
        except Exception:
            return None

        if t0.lower() == quote.lower():
            reserve_quote = r0
            reserve_token = r1
        else:
            reserve_quote = r1
            reserve_token = r0

        # quote -> USD
        quote_usd = self._quote_to_usd(reserve_quote, quote_dec, quote_sym) or 0

        # V2 两侧等价（价格 = r_quote / r_token，不需要额外调用）
        # 所以 token 侧 USD 价值 = quote 侧 USD 价值（恒成立）
        tvl = quote_usd * 2
        return tvl if tvl > 0 else None

    def _quote_to_usd(self, wei_amount: int, decimals: int, symbol: str) -> Optional[float]:
        amount = wei_amount / (10 ** decimals)
        if symbol == "USDT":
            return amount
        if symbol == "WBNB":
            bnb_usd = (self._bnb_price_ref or {}).get("price", 0) if self._bnb_price_ref else 0
            if not bnb_usd:
                bnb_usd = 600   # fallback
            return amount * bnb_usd
        return None

    # =====================================================================
    # 顶层：完整扫描（BSC 池子反推策略）
    # =====================================================================
    async def run_once(self, rt: RuntimeConfig) -> list[dict]:
        """
        新策略：从BSC PancakeSwap热门池子出发 → 匹配币安USDT-M合约 → 算基差

        步骤：
          1. 拉 BSC PancakeSwap 热门池子 top_pools（V3 + V2 + Infinity 都包含）
          2. 对每个池子：获取 base_token symbol
          3. 查这个 symbol 是否有币安 USDT-M 永续合约
          4. 两者都有 → 候选
          5. 按正基差降序排，留前 top_n_gainers 个
        """
        await DB.log_event("info", "--- Scan start (pool-first strategy) ---")

        # 1) 拉币安 USDT-M 永续合约列表（作为快速查找集）
        cex_symbols = await self._fetch_binance_perp_symbols()
        if not cex_symbols:
            await DB.log_event("error", "Failed to load Binance perp list")
            return []
        await DB.log_event("info", f"Binance USDT-M perps: {len(cex_symbols)}")

        # 2) 拉 BSC PancakeSwap 热门池子
        top_pools = await self._fetch_bsc_top_pools(limit=100)
        await DB.log_event("info", f"BSC top pools: {len(top_pools)}")

        # 3) 匹配并建候选
        confirmed: list[dict] = []
        skipped: list[str] = []
        seen_symbols: set[str] = set()    # 去重：一个币安symbol只留一个(TVL最大的)池子

        for p in top_pools:
            base_symbol = (p.get("base_symbol") or "").upper()
            if not base_symbol or base_symbol in seen_symbols:
                continue
            cex_symbol = f"{base_symbol}USDT"
            if cex_symbol not in cex_symbols:
                skipped.append(f"{base_symbol}(no_cex_perp)")
                continue

            # 过滤 fee
            fee_bps = p.get("pool_fee_bps") or 2500
            if fee_bps > rt.max_pool_fee_bps:
                skipped.append(f"{base_symbol}(fee_too_high:{fee_bps})")
                continue

            # 过滤 TVL
            tvl = p.get("pool_tvl_usd") or 0
            if tvl < rt.min_pool_tvl_usd:
                skipped.append(f"{base_symbol}(tvl_low:{tvl:.0f})")
                continue

            # 读 token decimals
            try:
                token_cs = Web3.to_checksum_address(p["base_token"])
                token_dec = await asyncio.to_thread(
                    self.w3.eth.contract(address=token_cs, abi=ERC20_ABI).functions.decimals().call
                )
            except Exception as e:
                skipped.append(f"{base_symbol}(decimals_err)")
                continue

            cand = {
                "symbol": cex_symbol,
                "base_asset": base_symbol,
                "token_address": token_cs,
                "pool_address": Web3.to_checksum_address(p["pool_address"]),
                "pool_fee": fee_bps,
                "pool_fee_pct": p.get("pool_fee_pct") or (fee_bps / 100),
                "pool_tvl_usd": tvl,
                "pool_24h_vol_usd": p.get("volume_24h_usd"),
                "decimals": token_dec,
                "change_24h_pct": None,     # 不再从涨幅榜来
                "last_cex_price": None,
                "last_dex_price": None,
                "last_basis_pct": None,
                "pool_version": p.get("pool_version", "v3"),
                "quote_token": p.get("quote_token"),
                "source": "bsc-pool",
            }
            await DB.upsert_candidate(cand)
            confirmed.append(cand)
            seen_symbols.add(base_symbol)

            await DB.log_event(
                "info",
                f"OK {cex_symbol} [{cand['pool_version'].upper()}] "
                f"TVL=${tvl:,.0f} fee={cand['pool_fee_pct']:.2f}% "
                f"pool={cand['pool_address'][:10]}..."
            )

        if skipped:
            await DB.log_event("info", f"Skipped: {', '.join(skipped[:25])}")

        # 空扫描不覆盖，非空时清理老的
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

    async def _fetch_binance_perp_symbols(self) -> set[str]:
        """拉币安 USDT-M 永续合约所有交易对 symbol，例如 {'BTCUSDT', 'ETHUSDT', ...}"""
        session = await self._get_session()
        url = f"{BINANCE_FUTURES_REST}/fapi/v1/exchangeInfo"
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return set()
                data = await r.json()
        except Exception as e:
            await DB.log_event("error", f"exchangeInfo err: {e}")
            return set()

        result = set()
        for s in data.get("symbols", []):
            if (s.get("contractType") == "PERPETUAL"
                and s.get("quoteAsset") == "USDT"
                and s.get("status") == "TRADING"):
                result.add(s["symbol"])
        return result

    async def _fetch_bsc_top_pools(self, limit: int = 100) -> list[dict]:
        """
        从 GeckoTerminal 拉 BSC 热门 PancakeSwap 池子。
        返回每个池子的 {base_symbol, base_token, pool_address, pool_fee_bps, pool_fee_pct,
                    pool_tvl_usd, volume_24h_usd, pool_version, quote_token}
        """
        session = await self._get_session()
        results: list[dict] = []
        pages_needed = max(1, (limit + 19) // 20)  # GeckoTerminal 每页 20

        for page in range(1, pages_needed + 1):
            url = f"{GECKO_TERMINAL_REST}/networks/bsc/pools"
            params = {"page": page, "sort": "h24_volume_usd_desc", "include": "base_token,quote_token,dex"}
            try:
                async with session.get(url, params=params) as r:
                    if r.status != 200:
                        break
                    data = await r.json()
            except Exception:
                break

            pools_raw = (data or {}).get("data", [])
            if not pools_raw:
                break

            for p in pools_raw:
                info = self._parse_pool_entry(p)
                if info:
                    results.append(info)
                if len(results) >= limit:
                    break
            if len(results) >= limit:
                break
        return results

    def _parse_pool_entry(self, p: dict) -> Optional[dict]:
        """解析 GeckoTerminal 一个 pool 条目 → 内部结构"""
        attr = p.get("attributes", {}) or {}
        rels = p.get("relationships", {}) or {}

        dex_id = (rels.get("dex", {}).get("data", {}) or {}).get("id", "").lower()
        if "pancakeswap" not in dex_id:
            return None

        # 分 version
        if "v3" in dex_id:
            pool_version = "v3"
        elif "v2" in dex_id:
            pool_version = "v2"
        elif "infinity-clmm" in dex_id:
            pool_version = "infinity-clmm"
        elif "infinity-lbamm" in dex_id:
            pool_version = "infinity-lbamm"
        elif "infinity" in dex_id:
            pool_version = "infinity"
        else:
            return None   # pancakeswap-stable 等暂不支持

        pool_addr_raw = (attr.get("address") or "").strip()
        if not pool_addr_raw.startswith("0x") or len(pool_addr_raw) != 42:
            return None

        # base/quote token
        base_id = (rels.get("base_token", {}).get("data", {}) or {}).get("id", "")
        quote_id = (rels.get("quote_token", {}).get("data", {}) or {}).get("id", "")
        def _strip(x):
            for pfx in ("bsc_", "bnb_", "bscmainnet_"):
                if x.startswith(pfx):
                    return x[len(pfx):]
            return x
        base_addr = _strip(base_id).lower()
        quote_addr_raw = _strip(quote_id).lower()
        if not base_addr.startswith("0x") or not quote_addr_raw.startswith("0x"):
            return None

        # 只支持 USDT / WBNB 计价
        if quote_addr_raw == USDT.lower():
            quote_sym = "USDT"
        elif quote_addr_raw == WBNB.lower():
            quote_sym = "WBNB"
        else:
            # GeckoTerminal 里 base/quote 可能被它自己颠倒（它把小价值token作为base）
            # 如果 base 是 USDT/WBNB 而 quote 不是，说明它反了，我们交换
            if base_addr == USDT.lower():
                quote_sym = "USDT"
                base_addr, quote_addr_raw = quote_addr_raw, base_addr
            elif base_addr == WBNB.lower():
                quote_sym = "WBNB"
                base_addr, quote_addr_raw = quote_addr_raw, base_addr
            else:
                return None   # 非 USDT/WBNB 计价的池子跳过

        # 从 name 里解析 symbol 和 fee
        # name 格式例: "TRADOOR / USDT 0.01%"
        name = attr.get("name") or ""
        base_symbol = None
        if "/" in name:
            base_symbol = name.split("/")[0].strip().upper()

        fee_bps, fee_pct = self._parse_fee_from_name(name)
        # Infinity / V3 可能都有 fee, V2 固定 0.25%
        if fee_bps is None:
            if pool_version == "v2":
                fee_bps, fee_pct = 2500, 0.25
            else:
                fee_bps, fee_pct = 2500, 0.25   # 默认 0.25%

        # TVL 与 volume
        try:
            tvl = float(attr.get("reserve_in_usd") or 0)
        except Exception:
            tvl = 0
        try:
            vol_24h = float(attr.get("volume_usd", {}).get("h24") or 0)
        except Exception:
            vol_24h = 0

        return {
            "base_symbol": base_symbol,
            "base_token": Web3.to_checksum_address(base_addr) if base_addr.startswith("0x") and len(base_addr)==42 else None,
            "pool_address": pool_addr_raw,
            "pool_version": pool_version,
            "pool_fee_bps": fee_bps,
            "pool_fee_pct": fee_pct,
            "pool_tvl_usd": tvl,
            "volume_24h_usd": vol_24h,
            "quote_token": Web3.to_checksum_address(USDT) if quote_sym == "USDT" else Web3.to_checksum_address(WBNB),
            "quote_symbol": quote_sym,
        }

    @staticmethod
    def _parse_fee_from_name(name: str) -> tuple:
        """
        从池子 name 里解析 fee，比如：
          "TRADOOR / USDT 0.01%" → (100, 0.01)
          "CAKE / WBNB 0.25%"    → (2500, 0.25)
          "XYZ / USDT 1%"        → (10000, 1.0)
        返回 (fee_bps, fee_pct) 或 (None, None) 如解析失败
        fee_bps 单位：每百万分之，2500=0.25%, 10000=1%
        """
        import re
        m = re.search(r"(\d+(?:\.\d+)?)\s*%", name)
        if not m:
            return None, None
        try:
            pct = float(m.group(1))
            # V3 fee tiers: 0.01, 0.05, 0.25, 1.0
            bps = int(round(pct * 10000))
            return bps, pct
        except Exception:
            return None, None


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
