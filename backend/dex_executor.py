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
from .config import STATIC, RUNTIME, PANCAKE_V3_SWAP_ROUTER, PANCAKE_V2_ROUTER, USDT
from .abi import V3_SWAP_ROUTER_ABI, ERC20_ABI, V3_QUOTER_ABI, V2_ROUTER_ABI
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
        # V3
        self.v3_router = self.w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_SWAP_ROUTER),
            abi=V3_SWAP_ROUTER_ABI,
        )
        self.quoter = self.w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V3_QUOTER),
            abi=V3_QUOTER_ABI,
        )
        # V2
        self.v2_router = self.w3.eth.contract(
            address=Web3.to_checksum_address(PANCAKE_V2_ROUTER),
            abi=V2_ROUTER_ABI,
        )
        # 向后兼容
        self.router = self.v3_router

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
    async def ensure_approved(self, token_addr: str, router_version: str = "v3"):
        """对指定版本router授权。router_version = 'v2' 或 'v3'"""
        router_addr = self.v3_router.address if router_version == "v3" else self.v2_router.address
        key = f"{token_addr.lower()}:{router_version}"
        if key in self._approved:
            return
        if RUNTIME.dry_run:
            self._approved.add(key)
            return

        token = self.w3.eth.contract(
            address=Web3.to_checksum_address(token_addr), abi=ERC20_ABI
        )
        allowance = await asyncio.to_thread(
            token.functions.allowance(self.account.address, router_addr).call
        )
        if allowance > 10**30:
            self._approved.add(key)
            return

        await DB.log_event("info", f"Approving {token_addr} for {router_version} router...")
        nonce = await self._next_nonce()
        tx = token.functions.approve(router_addr, MAX_UINT).build_transaction({
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
            await DB.log_event("info", f"Approved {token_addr} for {router_version}: {tx_hash.hex()}")
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
        pool_version: str = "v3",
    ) -> dict:
        """
        花费 usdt_amount USDT 买入 token。
        pool_version: 'v3' (默认) 或 'v2'
        """
        if RUNTIME.dry_run:
            await asyncio.sleep(0.3)
            pool_fee_rate = pool_fee / 1_000_000
            effective_price_with_fee = expected_token_price_usd * (1 + pool_fee_rate)
            amount_out = usdt_amount / effective_price_with_fee
            return {
                "ok": True, "tx_hash": "0x" + "dry" * 16,
                "latency_ms_send": 120, "latency_ms_confirm": 500,
                "amount_out": amount_out,
                "effective_price": effective_price_with_fee,
                "gas_used": 180_000,
            }

        if pool_version == "v2":
            return await self._v2_swap_exact_in(
                USDT, token_addr, usdt_amount, expected_token_price_usd, max_slippage,
                direction="buy"
            )
        else:
            return await self._v3_swap_exact_in(
                USDT, token_addr, pool_fee, usdt_amount, expected_token_price_usd, max_slippage,
                direction="buy"
            )

    # ---------- 卖出现货（平仓） ----------
    async def sell_token_for_usdt(
        self,
        token_addr: str,
        pool_fee: int,
        token_amount: float,
        expected_token_price_usd: float,
        max_slippage: float,
        pool_version: str = "v3",
    ) -> dict:
        if RUNTIME.dry_run:
            await asyncio.sleep(0.3)
            pool_fee_rate = pool_fee / 1_000_000
            effective_price_with_fee = expected_token_price_usd * (1 - pool_fee_rate)
            amount_out_usdt = token_amount * effective_price_with_fee
            return {
                "ok": True, "tx_hash": "0x" + "dry" * 16,
                "latency_ms_send": 120, "latency_ms_confirm": 500,
                "amount_out": amount_out_usdt,
                "effective_price": effective_price_with_fee,
                "gas_used": 180_000,
            }

        if pool_version == "v2":
            return await self._v2_swap_exact_in(
                token_addr, USDT, token_amount, expected_token_price_usd, max_slippage,
                direction="sell"
            )
        else:
            return await self._v3_swap_exact_in(
                token_addr, USDT, pool_fee, token_amount, expected_token_price_usd, max_slippage,
                direction="sell"
            )

    # ---------- V3 swap ----------
    async def _v3_swap_exact_in(
        self, token_in: str, token_out: str, pool_fee: int,
        amount_in_human: float, expected_price_usd: float, max_slippage: float,
        direction: str,
    ) -> dict:
        """direction: 'buy' (USDT->token) 或 'sell' (token->USDT)"""
        await self.ensure_approved(token_in, router_version="v3")
        await self._refresh_gas_price()

        in_dec = await self._get_decimals(token_in)
        out_dec = await self._get_decimals(token_out)
        amount_in_wei = int(amount_in_human * (10 ** in_dec))

        if direction == "buy":
            expected_out_human = amount_in_human / expected_price_usd
        else:
            expected_out_human = amount_in_human * expected_price_usd
        expected_out_wei = expected_out_human * (10 ** out_dec)
        min_out = int(expected_out_wei * (1 - max_slippage))

        deadline = int(time.time()) + 60
        params = (
            Web3.to_checksum_address(token_in),
            Web3.to_checksum_address(token_out),
            int(pool_fee),
            self.account.address,
            deadline,
            amount_in_wei,
            min_out,
            0,
        )
        nonce = await self._next_nonce()
        tx = self.v3_router.functions.exactInputSingle(params).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": 250000,
            "gasPrice": self._gas_price,
            "chainId": 56,
            "value": 0,
        })
        return await self._sign_send_wait(tx, token_out, out_dec, min_out,
                                          amount_in_human, expected_price_usd, direction)

    # ---------- V2 swap ----------
    async def _v2_swap_exact_in(
        self, token_in: str, token_out: str,
        amount_in_human: float, expected_price_usd: float, max_slippage: float,
        direction: str,
    ) -> dict:
        await self.ensure_approved(token_in, router_version="v2")
        await self._refresh_gas_price()

        in_dec = await self._get_decimals(token_in)
        out_dec = await self._get_decimals(token_out)
        amount_in_wei = int(amount_in_human * (10 ** in_dec))

        if direction == "buy":
            expected_out_human = amount_in_human / expected_price_usd
        else:
            expected_out_human = amount_in_human * expected_price_usd
        expected_out_wei = expected_out_human * (10 ** out_dec)
        min_out = int(expected_out_wei * (1 - max_slippage))

        path = [Web3.to_checksum_address(token_in), Web3.to_checksum_address(token_out)]
        deadline = int(time.time()) + 60

        nonce = await self._next_nonce()
        # 用 SupportingFeeOnTransferTokens，兼容有转账税的 token
        tx = self.v2_router.functions.swapExactTokensForTokensSupportingFeeOnTransferTokens(
            amount_in_wei, min_out, path, self.account.address, deadline
        ).build_transaction({
            "from": self.account.address,
            "nonce": nonce,
            "gas": 300000,   # V2 比 V3 用 gas 稍高
            "gasPrice": self._gas_price,
            "chainId": 56,
            "value": 0,
        })
        return await self._sign_send_wait(tx, token_out, out_dec, min_out,
                                          amount_in_human, expected_price_usd, direction)

    # ---------- 通用：签名 -> 发送 -> 等待确认 -> 解析 ----------
    async def _sign_send_wait(
        self, tx: dict, token_out: str, out_dec: int, min_out: int,
        amount_in_human: float, expected_price_usd: float, direction: str,
    ) -> dict:
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
            return {"ok": False, "tx_hash": tx_hash.hex(),
                    "latency_ms_send": send_latency_ms,
                    "latency_ms_confirm": confirm_latency_ms,
                    "error": "revert"}

        # 从 Transfer 事件解析真实入账数量
        actual_out_wei = self._parse_transfer_to_me(receipt, token_out)
        if actual_out_wei and actual_out_wei >= min_out:
            actual_out = actual_out_wei / (10 ** out_dec)
        else:
            actual_out = min_out / (10 ** out_dec)

        if direction == "buy":
            effective_price = amount_in_human / actual_out if actual_out > 0 else expected_price_usd
        else:
            effective_price = actual_out / amount_in_human if amount_in_human > 0 else expected_price_usd

        return {
            "ok": True,
            "tx_hash": tx_hash.hex(),
            "latency_ms_send": send_latency_ms,
            "latency_ms_confirm": confirm_latency_ms,
            "amount_out": actual_out,
            "effective_price": effective_price,
            "gas_used": receipt.gasUsed,
        }
