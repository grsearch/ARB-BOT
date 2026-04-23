"""
套利决策引擎 - 核心状态机。

新版改进：
1. 全程打时间戳（t_signal -> t_cex_sent -> t_cex_filled -> t_dex_sent -> t_dex_confirmed）
   方便用户在 Dashboard 上精确分析每个阶段的耗时
2. 实际成交价（effective price）而非触发价计算 PnL
3. 含完整手续费：CEX taker、DEX pool fee、BSC gas
4. 净 PnL = gross_pnl - all_fees

执行顺序（先CEX后DEX）：
  t0: _signal - 检测到机会
  t1: cex.open_short() 发送
  t2: cex成交确认 -> 立刻发 DEX
  t3: dex.buy() 已签名发送
  t4: dex receipt 确认
  total_latency = t4 - t0

平仓时 DEX + CEX 并行发送，两者都确认算完成。
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Optional
from web3 import Web3
from .config import RUNTIME, TYPICAL_SWAP_GAS_UNITS
from .db import DB


def _ms() -> int:
    return int(time.time() * 1000)


@dataclass
class Position:
    symbol: str
    token_address: str
    pool_address: str
    pool_fee: int          # bps: 2500=0.25%
    pool_fee_pct: float    # 0.0025
    pool_version: str      # 'v2' | 'v3'

    ts_open: int
    entry_basis: float
    position_usdt: float

    # CEX
    cex_avg_entry: float
    cex_filled_qty: float
    cex_order_id_open: str
    cex_fee_open_usdt: float

    # DEX
    dex_entry_price: float
    dex_amount_token: float
    dex_tx_open: str
    dex_fee_open_usdt: float
    gas_fee_open_usdt: float

    # 时间戳
    t_signal: int
    t_cex_sent_open: int
    t_cex_filled_open: int
    t_dex_sent_open: int
    t_dex_confirmed_open: int

    trade_id: int
    status: str = "open"


class ArbEngine:
    def __init__(self, cex_executor, dex_executor, dex_feed, cex_feed, bnb_price_ref: dict, ws_broadcaster=None):
        self.cex_exec = cex_executor
        self.dex_exec = dex_executor
        self.dex_feed = dex_feed
        self.cex_feed = cex_feed
        self.bnb_price_ref = bnb_price_ref
        self.ws_broadcaster = ws_broadcaster

        self.prices_cex: dict[str, dict] = {}
        self.prices_dex: dict[str, dict] = {}
        self.candidates: dict[str, dict] = {}
        self.positions: dict[str, Position] = {}
        self._busy: set[str] = set()
        # 卡位 token 重试队列：DEX 卖出失败的 token 等待重试
        # key=symbol, value={symbol, token_address, pool_fee, pool_version, amount, ...}
        self.pending_unwind: dict[str, dict] = {}

    # ---------- 外部入口 ----------
    async def on_cex_price(self, symbol: str, bid: float, ask: float, ts: int):
        mid = (bid + ask) / 2
        self.prices_cex[symbol] = {"bid": bid, "ask": ask, "mid": mid, "ts": ts}
        if symbol == "BNBUSDT":
            self.bnb_price_ref["price"] = mid
        await self._check(symbol)
        await self._broadcast_price(symbol)

    async def on_dex_price(self, symbol: str, price: float, ts: int, source: str):
        self.prices_dex[symbol] = {"price": price, "ts": ts, "source": source}
        await self._check(symbol)
        await self._broadcast_price(symbol)

    async def on_candidates_update(self, candidates: list[dict]):
        # 空列表不覆盖（防止扫描临时失败导致 engine 候选丢失）
        if not candidates:
            return
        self.candidates = {c["symbol"]: c for c in candidates}
        # CEX 订阅：所有候选 + BNBUSDT
        symbols = set(self.candidates.keys()) | {"BNBUSDT"}
        await self.cex_feed.update_subscriptions(symbols)
        await self.dex_feed.update_candidates(candidates)

    async def _broadcast_price(self, symbol: str):
        if not self.ws_broadcaster:
            return
        cex = self.prices_cex.get(symbol, {})
        dex = self.prices_dex.get(symbol, {})
        cex_mid = cex.get("mid")
        dex_px = dex.get("price")
        basis = None
        if cex_mid and dex_px and dex_px > 0:
            basis = (cex_mid - dex_px) / dex_px
        await self.ws_broadcaster({
            "type": "price",
            "symbol": symbol,
            "cex_bid": cex.get("bid"),
            "cex_ask": cex.get("ask"),
            "cex_mid": cex_mid,
            "dex": dex_px,
            "dex_source": dex.get("source"),
            "basis_pct": basis * 100 if basis is not None else None,
            "ts": _ms(),
        })

    # ---------- 决策 ----------
    async def _check(self, symbol: str):
        if not RUNTIME.enabled or symbol in self._busy:
            return
        cex = self.prices_cex.get(symbol)
        dex = self.prices_dex.get(symbol)
        if not cex or not dex or dex["price"] <= 0:
            return

        cex_mid = cex["mid"]
        dex_px = dex["price"]
        basis = (cex_mid - dex_px) / dex_px

        # 已持仓：判断平仓
        if symbol in self.positions:
            if basis <= RUNTIME.exit_threshold:
                await self._close_position(symbol, basis)
            return

        # 未持仓：判断开仓
        if basis >= RUNTIME.entry_threshold:
            if len(self.positions) >= RUNTIME.max_concurrent_positions:
                return
            cand = self.candidates.get(symbol)
            if not cand:
                return
            await self._open_position(symbol, basis, cex_mid, dex_px, cand)

    # ---------- 手续费/PnL 辅助 ----------
    def _calc_gas_cost_usdt(self, gas_used: int) -> float:
        """
        BSC gas cost 换算成 USDT：
          gas_used * gas_price(wei) -> BNB -> USDT
        使用实时 BNB/USDT 价。
        """
        try:
            gas_price = self.dex_exec._gas_price   # wei
            bnb_price = self.bnb_price_ref.get("price", 0)
            if bnb_price <= 0:
                bnb_price = 600  # fallback
            bnb_cost = gas_used * gas_price / 1e18
            return bnb_cost * bnb_price
        except Exception:
            return 0.0

    # ---------- 开仓 ----------
    async def _open_position(self, symbol: str, basis: float, cex_mid: float, dex_px: float, cand: dict):
        # 黑名单双重拦截（scanner已过滤，这里兜底）
        bl_raw = getattr(RUNTIME, "symbol_blacklist", "") or ""
        blacklist = {s.strip().upper() for s in bl_raw.split(",") if s.strip()}
        if symbol.upper() in blacklist:
            await DB.log_event("info", f"SKIP {symbol}: blacklisted")
            return

        # Infinity 池子当前不支持自动交易（仅监控基差）
        pool_version = cand.get("pool_version", "v3")
        if pool_version.startswith("infinity"):
            await DB.log_event(
                "info",
                f"SKIP trade {symbol}: pool is {pool_version} "
                f"(仅监控，下一版本支持 Universal Router 集成)"
            )
            return

        self._busy.add(symbol)
        try:
            t_signal = _ms()
            await DB.log_event(
                "info",
                f"OPEN trigger {symbol} basis={basis*100:.2f}% cex={cex_mid:.6g} dex={dex_px:.6g} "
                f"pool_tvl=${cand.get('pool_tvl_usd',0):,.0f}"
            )

            # 1) 币安开空（market sell）
            t_cex_sent = _ms()
            cex_res = await self.cex_exec.open_short(
                symbol, RUNTIME.position_usdt, cex_mid, RUNTIME.leverage
            )
            t_cex_filled = _ms()
            if not cex_res.get("ok"):
                await DB.log_event("error", f"CEX open FAIL {symbol}: {cex_res.get('error')}")
                return

            cex_avg = cex_res["avg_price"]
            cex_qty = cex_res["filled"]
            cex_notional = cex_avg * cex_qty
            cex_fee = cex_notional * RUNTIME.cex_taker_fee

            # 2) DEX 买入（按 CEX 实际成交的名义价值）
            t_dex_sent = _ms()
            dex_res = await self.dex_exec.buy_token_with_usdt(
                cand["token_address"], cand["pool_fee"],
                cex_notional, dex_px, RUNTIME.max_slippage,
                pool_version=pool_version,
            )
            t_dex_confirmed = _ms()

            if not dex_res.get("ok"):
                # DEX 失败 -> 紧急平CEX空头（含重试）
                await DB.log_event("error",
                    f"DEX open FAIL {symbol}: {dex_res.get('error')} -> emergency CEX cover")

                cov = None
                for attempt in range(3):   # 最多重试3次，每次间隔500ms
                    cov = await self.cex_exec.close_short(symbol, cex_qty, cex_mid)
                    if cov.get("ok"):
                        break
                    await DB.log_event("warn",
                        f"Emergency close retry {attempt+1}/3 for {symbol}: {cov.get('error')}")
                    await asyncio.sleep(0.5)

                if cov and cov.get("ok"):
                    cov_notional = cov["avg_price"] * cov["filled"]
                    cov_fee = cov_notional * RUNTIME.cex_taker_fee
                    loss = (cov["avg_price"] - cex_avg) * cex_qty + cex_fee + cov_fee
                    await DB.log_event("warn",
                        f"Emergency close done for {symbol}, loss≈{loss:.4f} USDT")
                else:
                    # 严重：CEX已开空但平仓也失败，人工介入
                    await DB.log_event("error",
                        f"!!! CRITICAL {symbol}: CEX short open but emergency close FAILED after 3 retries. "
                        f"Manual action required. cex_avg={cex_avg} qty={cex_qty} "
                        f"err={(cov or {}).get('error')}")
                return

            dex_eff_price = dex_res["effective_price"]
            dex_amount_token = dex_res["amount_out"]
            dex_fee = cex_notional * cand.get("pool_fee_pct", 0) / 100  # pool_fee_pct 是0.25这种百分数形式
            # 修正：pool_fee_pct 已经是百分数（0.25 表示 0.25%），所以 /100 才是小数
            gas_used = dex_res.get("gas_used", TYPICAL_SWAP_GAS_UNITS)
            gas_fee = self._calc_gas_cost_usdt(gas_used)

            # 3) 延迟统计
            total_latency_open = t_dex_confirmed - t_signal
            cex_fill_lat = t_cex_filled - t_cex_sent
            dex_send_lat = t_dex_sent - t_cex_filled
            dex_confirm_lat = t_dex_confirmed - t_dex_sent
            await DB.log_latency("cex_fill_open", cex_fill_lat, symbol)
            await DB.log_latency("dex_send_open", dex_send_lat, symbol, dex_res.get("tx_hash", ""))
            await DB.log_latency("dex_confirm_open", dex_confirm_lat, symbol, dex_res.get("tx_hash", ""))
            await DB.log_latency("total_open", total_latency_open, symbol)

            # 4) 持久化
            trade_id = await DB.execute(
                """INSERT INTO trades
                (ts_open, symbol, token_address, pool_address, pool_fee, side,
                 entry_basis_pct, position_usdt, leverage,
                 dex_entry_price, cex_entry_price, dex_entry_amount, cex_entry_qty,
                 dex_tx_hash_open, cex_order_id_open,
                 t_signal, t_cex_sent_open, t_cex_filled_open,
                 t_dex_sent_open, t_dex_confirmed_open,
                 exec_latency_ms_open, cex_fill_latency_open,
                 dex_send_latency_open, dex_confirm_latency_open,
                 cex_fee_usdt, dex_fee_usdt, gas_fee_usdt,
                 dry_run, status)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (t_signal, symbol, cand["token_address"], cand["pool_address"],
                 cand["pool_fee"], "arb",
                 basis * 100, RUNTIME.position_usdt, RUNTIME.leverage,
                 dex_eff_price, cex_avg, dex_amount_token, cex_qty,
                 dex_res.get("tx_hash", ""), cex_res.get("order_id", ""),
                 t_signal, t_cex_sent, t_cex_filled, t_dex_sent, t_dex_confirmed,
                 total_latency_open, cex_fill_lat, dex_send_lat, dex_confirm_lat,
                 cex_fee, dex_fee, gas_fee,
                 1 if RUNTIME.dry_run else 0, "open")
            )

            pos = Position(
                symbol=symbol, token_address=cand["token_address"],
                pool_address=cand["pool_address"], pool_fee=cand["pool_fee"],
                pool_fee_pct=cand.get("pool_fee_pct", 0) / 100,
                pool_version=pool_version,
                ts_open=t_signal, entry_basis=basis,
                position_usdt=RUNTIME.position_usdt,
                cex_avg_entry=cex_avg, cex_filled_qty=cex_qty,
                cex_order_id_open=cex_res.get("order_id", ""),
                cex_fee_open_usdt=cex_fee,
                dex_entry_price=dex_eff_price,
                dex_amount_token=dex_amount_token,
                dex_tx_open=dex_res.get("tx_hash", ""),
                dex_fee_open_usdt=dex_fee,
                gas_fee_open_usdt=gas_fee,
                t_signal=t_signal, t_cex_sent_open=t_cex_sent,
                t_cex_filled_open=t_cex_filled,
                t_dex_sent_open=t_dex_sent,
                t_dex_confirmed_open=t_dex_confirmed,
                trade_id=trade_id,
            )
            self.positions[symbol] = pos

            await DB.log_event(
                "info",
                f"POSITION OPEN {symbol}: total={total_latency_open}ms "
                f"[cex_fill={cex_fill_lat} dex_send={dex_send_lat} dex_conf={dex_confirm_lat}] "
                f"cex={cex_avg:.6g} dex={dex_eff_price:.6g} "
                f"fees: cex={cex_fee:.4f} dex={dex_fee:.4f} gas={gas_fee:.4f}"
            )

            if self.ws_broadcaster:
                await self.ws_broadcaster({
                    "type": "position_open", "symbol": symbol,
                    "latency_ms": total_latency_open,
                    "cex_fill_ms": cex_fill_lat,
                    "dex_send_ms": dex_send_lat,
                    "dex_confirm_ms": dex_confirm_lat,
                    "basis_pct": basis * 100,
                })

            # 延迟超限：立即平仓
            if total_latency_open > RUNTIME.max_exec_latency_ms:
                await DB.log_event(
                    "warn",
                    f"{symbol} open latency {total_latency_open}ms > {RUNTIME.max_exec_latency_ms}ms, force close"
                )
                await asyncio.sleep(0.3)
                await self._close_position(symbol, basis, reason="latency_limit")
        finally:
            self._busy.discard(symbol)

    # ---------- 平仓 (DEX 优先串行) ----------
    async def _close_position(self, symbol: str, exit_basis: float, reason: str = "convergence"):
        """
        新策略：先 DEX 卖 token → 成功再 CEX 平空
        DEX 失败 → CEX 保持开空（对冲还在），token 进入 pending_unwind 重试队列
        """
        if symbol not in self.positions or symbol in self._busy:
            return
        self._busy.add(symbol)
        try:
            pos = self.positions[symbol]
            t_signal_close = _ms()
            await DB.log_event(
                "info",
                f"CLOSE trigger {symbol} basis={exit_basis*100:.2f}% reason={reason}"
            )

            dex_now = self.prices_dex.get(symbol, {}).get("price") or pos.dex_entry_price
            cex_now = self.prices_cex.get(symbol, {}).get("mid") or pos.cex_avg_entry

            # ===== Step 1: 先做 DEX sell (串行) =====
            t_dex_sent = _ms()
            dex_res = await self.dex_exec.sell_token_for_usdt(
                pos.token_address, pos.pool_fee, pos.dex_amount_token,
                dex_now, RUNTIME.max_slippage,
                pool_version=pos.pool_version
            )
            t_dex_done = _ms()
            ok_dex = isinstance(dex_res, dict) and dex_res.get("ok")

            # DEX 失败 → 不平 CEX，token 进重试队列
            if not ok_dex:
                err = (dex_res or {}).get("error", "unknown")
                tx_hash = (dex_res or {}).get("tx_hash", "")
                await DB.log_event(
                    "error",
                    f"CLOSE DEX sell FAIL {symbol}: {err} tx={tx_hash} "
                    f"→ CEX 保持空单，token 进入 pending_unwind 队列重试"
                )
                # 加入重试队列
                self.pending_unwind[symbol] = {
                    "symbol": symbol,
                    "token_address": pos.token_address,
                    "pool_fee": pos.pool_fee,
                    "pool_version": pos.pool_version,
                    "amount": pos.dex_amount_token,
                    "pool_fee_pct": pos.pool_fee_pct,
                    "trade_id": pos.trade_id,
                    "attempts": 0,
                    "first_stuck_at": _ms(),
                    "last_error": err,
                }
                # 记录 trade 为 stuck_dex 状态
                await DB.execute(
                    "UPDATE trades SET status=?, error=? WHERE id=?",
                    ("stuck_dex", f"DEX sell failed on close: {err}", pos.trade_id)
                )
                return

            # ===== Step 2: DEX 成功，继续 CEX 平空 =====
            t_cex_sent = _ms()
            cex_res = await self.cex_exec.close_short(
                symbol, pos.cex_filled_qty, cex_now
            )
            t_done = _ms()
            ok_cex = isinstance(cex_res, dict) and cex_res.get("ok")

            # 手续费和PnL
            dex_exit_price = dex_res["effective_price"]
            dex_exit_amount = dex_res["amount_out"]
            dex_notional_close = dex_res["amount_out"]
            dex_fee_close = dex_notional_close * pos.pool_fee_pct
            gas_used = dex_res.get("gas_used", TYPICAL_SWAP_GAS_UNITS)
            gas_fee_close = self._calc_gas_cost_usdt(gas_used)

            cex_fee_close = 0.0
            cex_exit_price = None
            gross_pnl = 0.0
            if ok_cex:
                cex_exit_price = cex_res["avg_price"]
                cex_exit_notional = cex_res["avg_price"] * cex_res["filled"]
                cex_fee_close = cex_exit_notional * RUNTIME.cex_taker_fee
                dex_leg = (dex_res["effective_price"] - pos.dex_entry_price) * pos.dex_amount_token
                cex_leg = (pos.cex_avg_entry - cex_res["avg_price"]) * pos.cex_filled_qty
                gross_pnl = dex_leg + cex_leg
            else:
                # DEX 成功但 CEX 失败 → 严重情况：token 已卖出，但空单还在
                # 此时反而要平空，否则敞口倒置
                err = (cex_res or {}).get("error", "unknown")
                await DB.log_event(
                    "error",
                    f"CLOSE CEX cover FAIL {symbol} AFTER DEX OK: {err} "
                    f"→ 重试 CEX 平空（DEX 已成交，敞口必须关）"
                )
                for attempt in range(5):
                    await asyncio.sleep(0.4)
                    cex_res = await self.cex_exec.close_short(symbol, pos.cex_filled_qty, cex_now)
                    if isinstance(cex_res, dict) and cex_res.get("ok"):
                        ok_cex = True
                        cex_exit_price = cex_res["avg_price"]
                        cex_exit_notional = cex_res["avg_price"] * cex_res["filled"]
                        cex_fee_close = cex_exit_notional * RUNTIME.cex_taker_fee
                        dex_leg = (dex_res["effective_price"] - pos.dex_entry_price) * pos.dex_amount_token
                        cex_leg = (pos.cex_avg_entry - cex_res["avg_price"]) * pos.cex_filled_qty
                        gross_pnl = dex_leg + cex_leg
                        t_done = _ms()
                        await DB.log_event("warn", f"CEX cover succeeded on retry {attempt+1}")
                        break
                    await DB.log_event("warn",
                        f"CEX cover retry {attempt+1}/5 for {symbol}: {(cex_res or {}).get('error')}")

                if not ok_cex:
                    await DB.log_event(
                        "error",
                        f"!!! CRITICAL {symbol}: DEX sold but CEX cover FAILED after 5 retries. "
                        f"Manual action required — close short on Binance manually."
                    )

            # 总手续费
            total_fees = (
                pos.cex_fee_open_usdt + pos.dex_fee_open_usdt + pos.gas_fee_open_usdt +
                cex_fee_close + dex_fee_close + gas_fee_close
            )
            net_pnl = gross_pnl - total_fees if (ok_cex and ok_dex) else None

            total_latency_close = t_done - t_signal_close
            status = "closed" if (ok_cex and ok_dex) else "error"
            err_str = None
            if status != "closed":
                err_str = f"cex={repr(cex_res) if not ok_cex else 'ok'}"

            def _safe_get(r, k, default=None):
                if isinstance(r, dict):
                    return r.get(k, default)
                return default

            await DB.execute(
                """UPDATE trades SET
                    ts_close=?, exit_basis_pct=?,
                    dex_exit_price=?, cex_exit_price=?,
                    dex_exit_amount=?, cex_exit_qty=?,
                    dex_tx_hash_close=?, cex_order_id_close=?,
                    t_signal_close=?, t_cex_sent_close=?, t_cex_filled_close=?,
                    t_dex_sent_close=?, t_dex_confirmed_close=?,
                    exec_latency_ms_close=?,
                    cex_fee_usdt = cex_fee_usdt + ?,
                    dex_fee_usdt = dex_fee_usdt + ?,
                    gas_fee_usdt = gas_fee_usdt + ?,
                    gross_pnl_usdt=?, realized_pnl_usdt=?,
                    status=?, error=?
                   WHERE id=?""",
                (t_done, exit_basis*100,
                 dex_exit_price, cex_exit_price,
                 dex_exit_amount, _safe_get(cex_res, "filled"),
                 _safe_get(dex_res, "tx_hash"),
                 _safe_get(cex_res, "order_id"),
                 t_signal_close, t_cex_sent,
                 t_done if ok_cex else None,
                 t_dex_sent,
                 t_dex_done,
                 total_latency_close,
                 cex_fee_close, dex_fee_close, gas_fee_close,
                 gross_pnl, net_pnl, status, err_str, pos.trade_id)
            )

            await DB.log_latency("total_close", total_latency_close, symbol)

            await DB.log_event(
                "info",
                f"POSITION CLOSE {symbol} reason={reason} "
                f"gross_pnl={gross_pnl:.4f} fees={total_fees:.4f} "
                f"net={net_pnl if net_pnl is not None else 'N/A'} "
                f"latency={total_latency_close}ms"
            )

            if self.ws_broadcaster:
                await self.ws_broadcaster({
                    "type": "position_close", "symbol": symbol,
                    "gross_pnl": round(gross_pnl, 4),
                    "net_pnl": round(net_pnl, 4) if net_pnl is not None else None,
                    "total_fees": round(total_fees, 4),
                    "latency_ms": total_latency_close,
                    "reason": reason,
                })

            del self.positions[symbol]
        finally:
            self._busy.discard(symbol)

    # ---------- 手动 ----------
    async def force_close_all(self, reason="manual"):
        for s in list(self.positions.keys()):
            await self._close_position(s, 0, reason=reason)

    # ---------- 卡位 token 自动重试 unwind ----------
    async def run_unwind_loop(self):
        """后台任务：扫 pending_unwind 队列，指数退避重试 DEX 卖出。

        退避节奏：第1次立即，第2次+60s，第3次+120s，第4次+240s，最多10次（~17分钟后放弃）
        """
        while True:
            try:
                await asyncio.sleep(30)
                if not self.pending_unwind:
                    continue
                now = _ms()
                for symbol in list(self.pending_unwind.keys()):
                    item = self.pending_unwind[symbol]
                    # 检查是否到退避时间
                    next_at = item.get("next_retry_at", 0)
                    if now < next_at:
                        continue
                    try:
                        await self._retry_unwind_one(symbol)
                    except Exception as e:
                        await DB.log_event("error", f"unwind retry err {symbol}: {e}")
            except asyncio.CancelledError:
                return
            except Exception as e:
                await DB.log_event("error", f"unwind loop err: {e}")

    async def _retry_unwind_one(self, symbol: str):
        item = self.pending_unwind.get(symbol)
        if not item:
            return
        item["attempts"] += 1

        # 指数退避：2^n 分钟，封顶20分钟
        backoff_sec = min(60 * (2 ** (item["attempts"] - 1)), 1200)
        item["next_retry_at"] = _ms() + backoff_sec * 1000

        MAX_ATTEMPTS = 10
        if item["attempts"] > MAX_ATTEMPTS:
            await DB.log_event(
                "error",
                f"!!! UNWIND GIVE UP {symbol} after {MAX_ATTEMPTS} attempts. "
                f"Token stuck. Use Wallet tab to manually sell."
            )
            return

        dex_px = self.prices_dex.get(symbol, {}).get("price") or 0
        if not dex_px:
            return

        # 关键：用链上实际余额，不用记录值（避免余额不足导致 revert）
        try:
            from .abi import ERC20_ABI
            token_ct = self.dex_exec.w3.eth.contract(
                address=Web3.to_checksum_address(item["token_address"]),
                abi=ERC20_ABI,
            )
            wallet_addr = self.dex_exec.account.address
            actual_wei = await asyncio.to_thread(
                token_ct.functions.balanceOf(wallet_addr).call
            )
            # token 小数
            dec_fn = token_ct.functions.decimals()
            dec = await asyncio.to_thread(dec_fn.call)
            actual_amount = actual_wei / (10 ** dec)
        except Exception as e:
            await DB.log_event("warn", f"UNWIND balance check fail {symbol}: {e}")
            actual_amount = item["amount"]   # fallback

        # 两者取小（防止超过实际余额）
        sell_amount = min(actual_amount, item["amount"])
        if sell_amount <= 0:
            await DB.log_event(
                "info",
                f"UNWIND {symbol}: wallet balance is 0, nothing to sell. Closing entry."
            )
            del self.pending_unwind[symbol]
            await DB.execute(
                "UPDATE trades SET status='closed', error='wallet balance 0 at unwind' WHERE id=?",
                (item["trade_id"],)
            )
            return

        await DB.log_event(
            "info",
            f"UNWIND retry {item['attempts']}/{MAX_ATTEMPTS} {symbol} "
            f"amount={sell_amount:.6g} (wallet={actual_amount:.6g})"
        )

        dex_res = await self.dex_exec.sell_token_for_usdt(
            item["token_address"], item["pool_fee"], sell_amount,
            dex_px, max(RUNTIME.max_slippage, 0.03),    # 重试时放宽滑点到至少3%
            pool_version=item["pool_version"]
        )

        if not (isinstance(dex_res, dict) and dex_res.get("ok")):
            item["last_error"] = (dex_res or {}).get("error", "unknown")
            await DB.log_event(
                "warn",
                f"UNWIND DEX sell still fail {symbol}: {item['last_error']} "
                f"(next retry in {backoff_sec}s)"
            )
            return

        # DEX 成功 → 补 CEX 平空（如果 pos 还在）
        pos = self.positions.get(symbol)
        if pos:
            cex_now = self.prices_cex.get(symbol, {}).get("mid") or pos.cex_avg_entry
            cex_res = None
            for attempt in range(5):
                cex_res = await self.cex_exec.close_short(
                    symbol, pos.cex_filled_qty, cex_now
                )
                if isinstance(cex_res, dict) and cex_res.get("ok"):
                    break
                await asyncio.sleep(0.4)
            if not (isinstance(cex_res, dict) and cex_res.get("ok")):
                await DB.log_event(
                    "error",
                    f"!!! {symbol} UNWIND: DEX sold ok, but CEX cover FAILED 5x. Manual action required."
                )
            else:
                await DB.log_event(
                    "info",
                    f"UNWIND OK {symbol}: DEX sold + CEX covered. Trade id={item['trade_id']}"
                )
            del self.positions[symbol]

        # 标记 trade 为 closed
        await DB.execute(
            "UPDATE trades SET status='closed', error=? WHERE id=?",
            (f"unwound after {item['attempts']} attempts", item["trade_id"])
        )
        del self.pending_unwind[symbol]
