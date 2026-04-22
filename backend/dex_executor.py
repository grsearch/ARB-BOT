"""
DEX 执行器 - 速度优化核心。

关键优化：
1. Web3 使用 HTTP Keep-Alive Session（复用 TCP 连接）
2. Nonce 预缓存 + 本地递增（避免每次 getTransactionCount 的 50-100ms）
3. Gas 参数预热：启动时就探测当前 gasPrice，运行中每 30s 刷新一次
4. 构建交易不等区块确认：sendRawTransaction 立刻返回 tx_hash
5. 永久 approve 最大值（一次性，避免每次都 approve）
6. V3 SwapRouter.exactInputSingle 直调（比 Universal Router 少 20-30% calldata）
7. 所有 read 操作都缓存结果（池子 token0/decimals 等）
"""
import asyncio
import time
from typing import Optional
from web3 import Web3, HTTPProvider
from eth_account import Account
from eth_account.signers.local import LocalAccount
from .config import STATIC, RUNTIME, PANCAKE_V3_SWAP_ROUTER, USDT
from .abi import V3_SWAP_ROUTER_ABI, ERC20_ABI, V3_QUOTER_ABI
from .abi import PANCAKE_V3_QUOTER
from .db import DB

# web3.py v6 用 geth_poa_middleware, v7 改名为 ExtraDataToPOAMiddleware
try:
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE
except ImportError:
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE

MAX_UINT = 2**256 - 1


class DEXExecutor:
    def __init__(self):
        # Keep-Alive Session
        import requests
        session = requests.Session()
        adapter = requests.adapters.HTTPAdapter(
            pool_connections=10, pool_maxsize=10, max_retries=0
        )
        session.mount("https://", adapter)
        session.mount("http://", adapter)

        self.w3 = Web3(HTTPProvider(
            STATIC.bsc_rpc_http,
            session=session,
            request_kwargs={"timeout": 5},
        ))
        self.w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)

        self.account: LocalAccount = Account.from_key(STATIC.wallet_private_key) \
            if STATIC.wallet_private_key else None
        self.router = self.w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_SWAP_ROUTER),
            abi=V3_SWAP_ROUTER_ABI,
        )
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_QUOTER),
            abi=V3_QUOTER_ABI,
        )

        # nonce 缓存
        self._nonce: Optional[int] = None
        self._nonce_lock = asyncio.Lock()

        # gas 缓存 (BSC使用 type 0 legacy gas)
        self._gas_price: int = self.w3.to_wei("1", "gwei")
        self._gas_refresh_ts = 0

        # 已 approve 的 token（针对 router）
        self._approved: set[str] = set()

        # token decimals 缓存
        self._token_decimals: dict[str, int] = {
            USDT.lower(): 18,  # BSC USDT 是18位小数
            WBNB.lower(): 18,
        }

    # ---------- 初始化 ----------
    async def init(self):
        if not self.account:
            await DB.log_event("warn", "No wallet loaded (DRY_RUN only)")
            return

        # 预填 nonce
        async with self._nonce_lock:
            self._nonce = await asyncio.to_thread(
                self.w3.eth.get_transaction_count, self.account.address
            )
        await self._refresh_gas_price(force=True)
        await DB.log_event("info", f"DEX ready: addr={self.account.address} nonce={self._nonce} gas={self._gas_price}")

    async def _refresh_gas_price(self, force=False):
        now = time.time()
        if not force and now - self._gas_refresh_ts < 30:
            return
        try:
            gp = await asyncio.to_thread(lambda: self.w3.eth.gas_price)
            # 对套利场景，我们愿意多付 30% 抢速度
            self._gas_price = int(gp * 1.3)
            self._gas_refresh_ts = now
        except Exception as e:
            await DB.log_event("warn", f"gas refresh fail: {e}")

    async def _next_nonce(self) -> int:
        async with self._nonce_lock:
            n = self._nonce
            self._nonce = (self._nonce or 0) + 1
            return n

    async def _reset_nonce_from_chain(self):
        """当交易失败时重新对齐nonce"""
        async with self._nonce_lock:
            self._nonce = await asyncio.to_thread(
                self.w3.eth.get_transaction_count, self.account.address
            )

    # ---------- approve ----------
    async def ensure_approved(self, token_addr: str):
        key = token_addr.lower()
        if key in self._approved:
            return
        if RUNTIME.dry_run:
            self._approved.add(key)
            return

        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        allowance = await asyncio.to_thread(
            token.functions.allowance(self.account.address, self.router.address).call
        )
        if allowance > 10**30:
            self._approved.add(key)
            return

        await DB.log_event("info", f"Approving {token_addr} for router...")
        nonce = await self._next_nonce()
        tx = token.functions.approve(self.router.address, MAX_UINT).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": 60000,
            "gasPrice": self._gas_price,
            "chainId": 56,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = await asyncio.to_thread(
            self.w3.eth.send_raw_transaction, signed.rawTransaction
        )
        receipt = await asyncio.to_thread(
            self.w3.eth.wait_for_transaction_receipt, tx_hash, 30
        )
        if receipt.status == 1:
            self._approved.add(key)
            await DB.log_event("info", f"Approved {token_addr}: {tx_hash.hex()}")
        else:
            await DB.log_event("error", f"Approve failed {token_addr}")
            await self._reset_nonce_from_chain()

    async def _get_decimals(self, token_addr: str) -> int:
        key = token_addr.lower()
        if key in self._token_decimals:
            return self._token_decimals[key]
        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        dec = await asyncio.to_thread(token.functions.decimals().call)
        self._token_decimals[key] = dec
        return dec

    def _parse_transfer_to_me(self, receipt, token_addr: str) -> Optional[int]:
        """
        从 receipt 的 logs 中解析 ERC20 Transfer(from, to=me, value)
        返回转入我钱包的 token 数量（wei）。
        """
        if not self.account:
            return None
        target = token_addr.lower()
        me_topic = "0x" + self.account.address[2:].lower().rjust(64, "0")
        transfer_topic = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
        best = None
        for log in receipt.logs:
            try:
                if log.address.lower() != target:
                    continue
                topics = [t.hex() if hasattr(t, "hex") else t for t in log.topics]
                if len(topics) < 3:
                    continue
                if topics[0].lower() != transfer_topic:
                    continue
                if topics[2].lower() != me_topic:
                    continue
                data = log.data.hex() if hasattr(log.data, "hex") else log.data
                if isinstance(data, str) and data.startswith("0x"):
                    value = int(data, 16)
                else:
                    value = int(data, 16) if isinstance(data, str) else int(data)
                # 取最后一笔（应是router -> me）
                best = value
            except Exception:
                continue
        return best

    # ---------- 价格估算（下单前的 min_out） ----------
    async def quote_exact_in(self, token_in: str, token_out: str, amount_in_wei: int, fee: int) -> int:
        """通过 Quoter 估算 amountOut。可选：直接用本地价格 * amountIn 算也行"""
        try:
            params = {
                "tokenIn": Web3.to_checksum_address(token_in),
                "tokenOut": Web3.to_checksum_address(token_out),
                "amountIn": amount_in_wei,
                "fee": fee,
                "sqrtPriceLimitX96": 0,
            }
            result = await asyncio.to_thread(
                self.quoter.functions.quoteExactInputSingle(params).call
            )
            return result[0]   # amountOut
        except Exception:
            return 0

    # ---------- 买入现货 ----------
    async def buy_token_with_usdt(
        self,
        token_addr: str,
        pool_fee: int,
        usdt_amount: float,
        expected_token_price_usd: float,
        max_slippage: float,
    ) -> dict:
        """
        花费 usdt_amount USDT 买入 token。
        返回 {ok, tx_hash, latency_ms_send, latency_ms_confirm, amount_out, ...}
        """
        if RUNTIME.dry_run:
            await asyncio.sleep(0.3)
            return {
                "ok": True, "tx_hash": "0x" + "dry" * 16,
                "latency_ms_send": 120, "latency_ms_confirm": 500,
                "amount_out": usdt_amount / expected_token_price_usd,
                "effective_price": expected_token_price_usd,
            }

        await self.ensure_approved(USDT)
        await self._refresh_gas_price()

        # USDT amount（BSC USDT 18位小数）
        usdt_dec = await self._get_decimals(USDT)
        amount_in_wei = int(usdt_amount * (10 ** usdt_dec))

        # 计算 min out
        token_dec = await self._get_decimals(token_addr)
        expected_out = (usdt_amount / expected_token_price_usd) * (10 ** token_dec)
        min_out = int(expected_out * (1 - max_slippage))

        deadline = int(time.time()) + 60
        params = (
            Web3.to_checksum_address(USDT),
            Web3.to_checksum_address(token_addr),
            int(pool_fee),
            self.account.address,
            deadline,
            amount_in_wei,
            min_out,
            0,  # sqrtPriceLimitX96=0 = 无限制（靠 min_out 防滑点）
        )

        nonce = await self._next_nonce()
        tx = self.router.functions.exactInputSingle(params).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": 250000,
            "gasPrice": self._gas_price,
            "chainId": 56,
            "value": 0,
        })
        signed = self.account.sign_transaction(tx)

        t0 = time.time()
        try:
            tx_hash = await asyncio.to_thread(
                self.w3.eth.send_raw_transaction, signed.rawTransaction
            )
        except Exception as e:
            await self._reset_nonce_from_chain()
            return {"ok": False, "error": f"send: {e}"}
        send_latency_ms = int((time.time() - t0) * 1000)

        # 等待确认（但不阻塞太久）
        t1 = time.time()
        try:
            receipt = await asyncio.to_thread(
                self.w3.eth.wait_for_transaction_receipt, tx_hash, 15
            )
        except Exception as e:
            return {"ok": False, "tx_hash": tx_hash.hex(),
                    "latency_ms_send": send_latency_ms,
                    "error": f"confirm_timeout: {e}"}
        confirm_latency_ms = int((time.time() - t1) * 1000)

        if receipt.status != 1:
            await self._reset_nonce_from_chain()
            return {"ok": False, "tx_hash": tx_hash.hex(),
                    "latency_ms_send": send_latency_ms,
                    "latency_ms_confirm": confirm_latency_ms,
                    "error": "revert"}

        # 解析 Transfer 事件得到真实入账 token 数量
        actual_out_wei = self._parse_transfer_to_me(receipt, token_addr)
        if actual_out_wei and actual_out_wei >= min_out:
            actual_out = actual_out_wei / (10 ** token_dec)
        else:
            actual_out = min_out / (10 ** token_dec)   # 保守回退

        return {
            "ok": True,
            "tx_hash": tx_hash.hex(),
            "latency_ms_send": send_latency_ms,
            "latency_ms_confirm": confirm_latency_ms,
            "amount_out": actual_out,
            "effective_price": usdt_amount / actual_out if actual_out > 0 else expected_token_price_usd,
            "gas_used": receipt.gasUsed,
        }

    # ---------- 卖出现货（平仓） ----------
    async def sell_token_for_usdt(
        self,
        token_addr: str,
        pool_fee: int,
        token_amount: float,
        expected_token_price_usd: float,
        max_slippage: float,
    ) -> dict:
        if RUNTIME.dry_run:
            await asyncio.sleep(0.3)
            return {
                "ok": True, "tx_hash": "0x" + "dry" * 16,
                "latency_ms_send": 120, "latency_ms_confirm": 500,
                "amount_out": token_amount * expected_token_price_usd,
                "effective_price": expected_token_price_usd,
            }

        await self.ensure_approved(token_addr)
        await self._refresh_gas_price()

        token_dec = await self._get_decimals(token_addr)
        usdt_dec = await self._get_decimals(USDT)
        amount_in_wei = int(token_amount * (10 ** token_dec))
        expected_out = token_amount * expected_token_price_usd * (10 ** usdt_dec)
        min_out = int(expected_out * (1 - max_slippage))

        deadline = int(time.time()) + 60
        params = (
            Web3.to_checksum_address(token_addr),
            Web3.to_checksum_address(USDT),
            int(pool_fee),
            self.account.address,
            deadline,
            amount_in_wei,
            min_out,
            0,
        )
        nonce = await self._next_nonce()
        tx = self.router.functions.exactInputSingle(params).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": 250000,
            "gasPrice": self._gas_price,
            "chainId": 56,
        })
        signed = self.account.sign_transaction(tx)
        t0 = time.time()
        try:
            tx_hash = await asyncio.to_thread(
                self.w3.eth.send_raw_transaction, signed.rawTransaction
            )
        except Exception as e:
            await self._reset_nonce_from_chain()
            return {"ok": False, "error": f"send: {e}"}
        send_latency_ms = int((time.time() - t0) * 1000)

        t1 = time.time()
        try:
            receipt = await asyncio.to_thread(
                self.w3.eth.wait_for_transaction_receipt, tx_hash, 15
            )
        except Exception as e:
            return {"ok": False, "tx_hash": tx_hash.hex(),
                    "latency_ms_send": send_latency_ms,
                    "error": f"confirm_timeout: {e}"}
        confirm_latency_ms = int((time.time() - t1) * 1000)

        if receipt.status != 1:
            await self._reset_nonce_from_chain()
            return {"ok": False, "tx_hash": tx_hash.hex(), "error": "revert"}

        # 解析 Transfer 事件得到真实入账 USDT 数量
        actual_out_wei = self._parse_transfer_to_me(receipt, USDT)
        if actual_out_wei and actual_out_wei >= min_out:
            actual_out_usdt = actual_out_wei / (10 ** usdt_dec)
        else:
            actual_out_usdt = min_out / (10 ** usdt_dec)

        return {
            "ok": True,
            "tx_hash": tx_hash.hex(),
            "latency_ms_send": send_latency_ms,
            "latency_ms_confirm": confirm_latency_ms,
            "amount_out": actual_out_usdt,
            "effective_price": actual_out_usdt / token_amount if token_amount > 0 else expected_token_price_usd,
            "gas_used": receipt.gasUsed,
        }
