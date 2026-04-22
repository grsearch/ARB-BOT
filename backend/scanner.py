"""
标的扫描器 - 三级过滤：

步骤1：【一次性/每天刷新】拉取币安支持 BSC 网络的币种列表
  - 使用已签名的 GET /sapi/v1/capital/config/getall
  - 筛选 networkList 中 network=='BSC' 且 depositEnable==True 的币
  - 这是"权威白名单"，确保不会选到同名诈骗币

步骤2：【每15分钟】拉取币安合约24h涨幅榜Top N
  - USDT本位永续
  - 与步骤1的BSC白名单求交集

步骤3：【每次扫描】对入围的币：
  3a. 用 GeckoTerminal 查该币在BSC的最佳池子（按流动性排序）
      - 优先 PancakeSwap V3
      - 过滤 TVL < min_pool_tvl_usd 的池子
  3b. 从链上 Factory.getPool 验证池子地址、fee、decimals
  3c. 记录 pool_address, pool_fee, pool_tvl_usd 等到 candidates 表

没通过任何一步的币种直接丢弃。
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
    PANCAKE_V3_FACTORY, V3_FEE_TIERS, USDT, WBNB,
)
from .abi import V3_FACTORY_ABI, V3_POOL_ABI, ERC20_ABI
from .db import DB


# 人工覆盖（若自动匹配错，可在此强制指定）
MANUAL_OVERRIDE = {
    # "RAVE": "0x97693439ea2f0ecdeb9135881e49f354656a911c",
}


class Scanner:
    def __init__(self, w3: Web3):
        self.w3 = w3
        self.factory = w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_FACTORY),
            abi=V3_FACTORY_ABI,
        )
        self._session: Optional[aiohttp.ClientSession] = None
        # BSC 白名单缓存
        self.bsc_coins: set[str] = set()       # {'CAKE', 'BNB', ...}
        self._bsc_coins_ts: float = 0
        # 合约地址缓存（每个币只查一次GeckoTerminal）
        self._address_cache: dict[str, dict] = {}  # base_asset -> {token_address, decimals, pool_address, pool_fee_bps, pool_tvl_usd, ...}

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

    # =================================================================
    # 步骤1：币安 BSC 白名单
    # =================================================================
    async def refresh_binance_bsc_whitelist(self):
        """
        每24小时刷新一次。
        通过已签名的 /sapi/v1/capital/config/getall 读取账户支持的币种列表，
        筛选出 networkList 里有 'BSC' 网络的币。
        """
        now = time.time()
        if self.bsc_coins and now - self._bsc_coins_ts < 86400:
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
                    await DB.log_event("error", f"capital/config/getall HTTP {r.status}: {text[:200]}")
                    return
                data = await r.json()
        except Exception as e:
            await DB.log_event("error", f"capital/config/getall error: {e}")
            return

        coins_set = set()
        cached_rows = []
        for c in data:
            coin = c.get("coin", "")
            if not coin or not c.get("trading", False):
                continue
            for n in c.get("networkList", []):
                if n.get("network") == "BSC" and n.get("depositEnable", False):
                    coins_set.add(coin)
                    # 币安deposit返回的contractAddress字段在部分币种上有，部分没有
                    contract_addr = n.get("contractAddress") or ""
                    cached_rows.append({
                        "coin": coin, "name": c.get("name", ""),
                        "trading": c.get("trading", False),
                        "contract_address": contract_addr.lower(),
                        "deposit_enable": n.get("depositEnable", False),
                        "withdraw_enable": n.get("withdrawEnable", False),
                    })
                    break

        self.bsc_coins = coins_set
        self._bsc_coins_ts = now
        await DB.cache_binance_bsc_coins(cached_rows)
        # 如果币安返回了contractAddress直接填入缓存
        for row in cached_rows:
            addr = row["contract_address"]
            if not addr:
                continue
            # 必须是 0x 开头的 40 位hex
            if not (addr.startswith("0x") and len(addr) == 42):
                continue
            try:
                cs = Web3.to_checksum_address(addr)
            except Exception:
                continue
            self._address_cache[row["coin"]] = {
                "token_address": cs,
                "source_contract": "binance_capital",
            }
        await DB.log_event("info", f"Binance BSC whitelist refreshed: {len(coins_set)} coins")

    # =================================================================
    # 步骤2：涨幅榜 Top N
    # =================================================================
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

    # =================================================================
    # 步骤3：GeckoTerminal 查 BSC 最佳池子
    # =================================================================
    async def find_best_pool_on_bsc(self, base_asset: str, min_tvl_usd: float) -> Optional[dict]:
        """
        返回 {
            token_address, decimals, pool_address, pool_fee_bps, pool_fee_pct,
            pool_tvl_usd, pool_24h_vol_usd, source
        } 或 None
        """
        # 检查人工覆盖
        if base_asset in MANUAL_OVERRIDE:
            token_addr = Web3.to_checksum_address(MANUAL_OVERRIDE[base_asset])
            return await self._build_pool_info_for_token(token_addr, min_tvl_usd)

        # 检查缓存（币安capital返回的contract）
        cached = self._address_cache.get(base_asset)
        if cached and cached.get("pool_address"):
            # 已经完整缓存
            return cached
        if cached and cached.get("token_address"):
            info = await self._build_pool_info_for_token(cached["token_address"], min_tvl_usd)
            if info:
                self._address_cache[base_asset] = info
            return info

        # 向 GeckoTerminal 查询：按 symbol 搜索 BSC 上的池子
        session = await self._get_session()
        # GeckoTerminal /search/pools?query=XXX&network=bsc
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

        # 过滤出：base symbol 完全匹配 + PancakeSwap V3 + TVL 足够
        candidates = []
        for p in pools:
            attr = p.get("attributes", {}) or {}
            rels = p.get("relationships", {}) or {}
            # 基币 symbol
            name = attr.get("name", "")  # e.g. "CAKE / WBNB 0.25%"
            base_part = name.split("/")[0].strip().upper() if "/" in name else ""
            if base_part != base_asset.upper():
                continue
            # DEX 是 PancakeSwap V3
            dex_id = (rels.get("dex", {}).get("data", {}) or {}).get("id", "").lower()
            if "pancakeswap" not in dex_id and "pancake" not in dex_id:
                continue
            if "v3" not in dex_id:
                continue
            # TVL
            tvl = float(attr.get("reserve_in_usd") or 0)
            if tvl < min_tvl_usd:
                continue
            candidates.append({
                "pool_address": attr.get("address"),
                "tvl_usd": tvl,
                "vol_24h_usd": float((attr.get("volume_usd") or {}).get("h24") or 0),
                "name": name,
                "base_token_id": (rels.get("base_token", {}).get("data", {}) or {}).get("id", ""),
            })

        if not candidates:
            return None

        best = max(candidates, key=lambda x: x["tvl_usd"])
        # GeckoTerminal base_token_id 格式通常是 'bsc_0x...'；如果不是，从池子合约自己读 token0/token1
        token_addr_str = best["base_token_id"]
        # 去除已知前缀
        for prefix in ("bsc_", "bnb_", "bscmainnet_"):
            if token_addr_str.startswith(prefix):
                token_addr_str = token_addr_str[len(prefix):]
                break

        pool_addr_raw = best["pool_address"]
        if not pool_addr_raw:
            return None

        try:
            pool_cs = Web3.to_checksum_address(pool_addr_raw)
        except Exception:
            return None

        # 尝试直接用解析到的地址验证
        token_addr = None
        if token_addr_str and token_addr_str.startswith("0x") and len(token_addr_str) == 42:
            try:
                token_addr = Web3.to_checksum_address(token_addr_str)
            except Exception:
                token_addr = None

        # 如果解析失败，从池子合约读 token0/token1 再判断哪个是 base
        if not token_addr:
            try:
                pc = self.w3.eth.contract(address=pool_cs, abi=V3_POOL_ABI)
                t0 = await asyncio.to_thread(pc.functions.token0().call)
                t1 = await asyncio.to_thread(pc.functions.token1().call)
            except Exception:
                return None
            usdt_l = USDT.lower()
            wbnb_l = WBNB.lower()
            if t0.lower() in (usdt_l, wbnb_l):
                token_addr = Web3.to_checksum_address(t1)
            elif t1.lower() in (usdt_l, wbnb_l):
                token_addr = Web3.to_checksum_address(t0)
            else:
                return None  # 不是 USDT/WBNB 计价池，跳过

        # 从链上验证池子真实fee与token对应关系
        info = await self._verify_pool_on_chain(pool_cs, token_addr)
        if not info:
            return None

        info["pool_tvl_usd"] = best["tvl_usd"]
        info["pool_24h_vol_usd"] = best["vol_24h_usd"]
        info["source"] = "binance_bsc_list+geckoterminal"
        self._address_cache[base_asset] = info
        return info

    async def _build_pool_info_for_token(self, token_addr: str, min_tvl_usd: float) -> Optional[dict]:
        """对已知的 token 地址，扫描4档 V3 fee 找最佳池（回退方案）"""
        usdt_cs = Web3.to_checksum_address(USDT)
        wbnb_cs = Web3.to_checksum_address(WBNB)

        best_pool = None
        best_liq = 0
        best_fee = None

        for quote in (usdt_cs, wbnb_cs):
            for fee in V3_FEE_TIERS:
                try:
                    pool = await asyncio.to_thread(
                        self.factory.functions.getPool(token_addr, quote, fee).call
                    )
                except Exception:
                    continue
                if not pool or int(pool, 16) == 0:
                    continue
                try:
                    pc = self.w3.eth.contract(
                        address=Web3.to_checksum_address(pool), abi=V3_POOL_ABI
                    )
                    liq = await asyncio.to_thread(pc.functions.liquidity().call)
                except Exception:
                    continue
                if liq > best_liq:
                    best_liq = liq
                    best_pool = pool
                    best_fee = fee

        if not best_pool:
            return None

        token_dec = await asyncio.to_thread(
            self.w3.eth.contract(address=token_addr, abi=ERC20_ABI).functions.decimals().call
        )

        # 简单TVL估算：我们用 GeckoTerminal 查一次
        tvl_usd = await self._gecko_pool_tvl(best_pool)
        if tvl_usd is not None and tvl_usd < min_tvl_usd:
            return None

        return {
            "token_address": token_addr,
            "decimals": token_dec,
            "pool_address": Web3.to_checksum_address(best_pool),
            "pool_fee_bps": best_fee,
            "pool_fee_pct": best_fee / 10000.0,   # 2500 -> 0.25
            "pool_tvl_usd": tvl_usd or 0,
            "pool_24h_vol_usd": 0,
            "source": "onchain_factory",
        }

    async def _verify_pool_on_chain(self, pool_address: str, expected_token: str) -> Optional[dict]:
        """验证池子确实包含预期token，并读取fee、decimals"""
        try:
            pc = self.w3.eth.contract(
                address=Web3.to_checksum_address(pool_address), abi=V3_POOL_ABI
            )
            token0 = await asyncio.to_thread(pc.functions.token0().call)
            token1 = await asyncio.to_thread(pc.functions.token1().call)
            fee = await asyncio.to_thread(pc.functions.fee().call)
        except Exception as e:
            await DB.log_event("warn", f"verify_pool fail {pool_address}: {e}")
            return None

        if expected_token.lower() not in (token0.lower(), token1.lower()):
            return None

        quote = token1 if token0.lower() == expected_token.lower() else token0
        if quote.lower() not in (USDT.lower(), WBNB.lower()):
            # 只做 token/USDT 或 token/WBNB 池（方便直接 swap）
            return None

        token_dec = await asyncio.to_thread(
            self.w3.eth.contract(
                address=Web3.to_checksum_address(expected_token), abi=ERC20_ABI
            ).functions.decimals().call
        )

        return {
            "token_address": Web3.to_checksum_address(expected_token),
            "decimals": token_dec,
            "pool_address": Web3.to_checksum_address(pool_address),
            "pool_fee_bps": fee,
            "pool_fee_pct": fee / 10000.0,
            "quote_token": quote.lower(),
        }

    async def _gecko_pool_tvl(self, pool_address: str) -> Optional[float]:
        """查单个池子的TVL"""
        session = await self._get_session()
        url = f"{GECKO_TERMINAL_REST}/networks/bsc/pools/{pool_address}"
        try:
            async with session.get(url) as r:
                if r.status != 200:
                    return None
                data = await r.json()
        except Exception:
            return None
        attr = ((data or {}).get("data", {}) or {}).get("attributes", {}) or {}
        return float(attr.get("reserve_in_usd") or 0)

    # =================================================================
    # 顶层：一次完整扫描
    # =================================================================
    async def run_once(self, rt: RuntimeConfig) -> list[dict]:
        await DB.log_event("info", "--- Scan start ---")

        # 1. 刷新BSC白名单（若过期）
        await self.refresh_binance_bsc_whitelist()
        if not self.bsc_coins:
            await DB.log_event("warn", "BSC whitelist empty, aborting scan")
            return []

        # 2. 涨幅榜
        gainers = await self.fetch_top_gainers(rt.top_n_gainers, rt.min_24h_gain_pct)
        await DB.log_event("info", f"Top gainers (>= {rt.min_24h_gain_pct*100:.1f}%): {len(gainers)}")

        # 3. 与 BSC 白名单求交集
        cross = [g for g in gainers if g["base_asset"] in self.bsc_coins]
        skipped_no_bsc = [g["base_asset"] for g in gainers if g["base_asset"] not in self.bsc_coins]
        if skipped_no_bsc:
            await DB.log_event("info", f"Skip (not on BSC): {','.join(skipped_no_bsc)}")

        # 4. 对每个币找最佳池子
        confirmed: list[dict] = []
        for g in cross:
            try:
                info = await self.find_best_pool_on_bsc(g["base_asset"], rt.min_pool_tvl_usd)
                if not info:
                    await DB.log_event("info", f"Skip (no BSC pool or TVL too low): {g['symbol']}")
                    continue

                cand = {
                    "symbol": g["symbol"],
                    "base_asset": g["base_asset"],
                    "token_address": info["token_address"],
                    "pool_address": info["pool_address"],
                    "pool_fee": info["pool_fee_bps"],
                    "pool_fee_pct": info["pool_fee_pct"],
                    "pool_tvl_usd": info.get("pool_tvl_usd", 0),
                    "pool_24h_vol_usd": info.get("pool_24h_vol_usd", 0),
                    "decimals": info["decimals"],
                    "change_24h_pct": g["change_24h_pct"],
                    "last_cex_price": g["last_cex_price"],
                    "last_dex_price": None,
                    "last_basis_pct": None,
                    "source": info.get("source", ""),
                }
                await DB.upsert_candidate(cand)
                confirmed.append(cand)
                await DB.log_event(
                    "info",
                    f"OK {g['symbol']} TVL=${info.get('pool_tvl_usd',0):,.0f} "
                    f"fee={info['pool_fee_pct']:.2f}% pool={info['pool_address'][:10]}..."
                )
            except Exception as e:
                await DB.log_event("error", f"Scan err for {g['symbol']}: {e}")

        # 清理不再在列表中的旧candidates
        kept = {c["symbol"] for c in confirmed}
        existing = await DB.fetchall("SELECT symbol FROM candidates", ())
        for row in existing:
            if row["symbol"] not in kept:
                await DB.execute("DELETE FROM candidates WHERE symbol=?", (row["symbol"],))

        await DB.log_event("info", f"--- Scan done: {len(confirmed)} candidates ---")
        return confirmed


async def run_scanner_loop(w3: Web3, on_update):
    scanner = Scanner(w3)
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
