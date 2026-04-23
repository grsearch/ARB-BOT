"""
主启动入口：
1. 初始化 DB
2. 启动各模块（scanner, cex_feed, dex_feed, engine, fastapi）
3. 串联事件流
"""
import asyncio
import signal
import uvicorn
from web3 import Web3, HTTPProvider

try:
    from web3.middleware import geth_poa_middleware as POA_MIDDLEWARE
except ImportError:
    from web3.middleware import ExtraDataToPOAMiddleware as POA_MIDDLEWARE

from .config import STATIC, RUNTIME
from .db import DB
from .api import build_app, WSHub
from .cex_feed import BinanceWSFeed
from .cex_executor import CEXExecutor
from .dex_executor import DEXExecutor
from .dex_feed import DexFeedManager
from .engine import ArbEngine
from .scanner import run_scanner_loop


async def amain():
    await DB.init()
    await DB.log_event("info", "===== Bot starting =====")

    # BNB/USD 参考价
    bnb_price_ref = {"price": 0.0}

    # 初始化 Web3
    w3 = Web3(HTTPProvider(STATIC.bsc_rpc_http, request_kwargs={"timeout": 5}))
    w3.middleware_onion.inject(POA_MIDDLEWARE, layer=0)

    # 执行器
    cex_exec = CEXExecutor()
    await cex_exec.init()

    dex_exec = DEXExecutor()
    await dex_exec.init()

    # WS Hub
    hub = WSHub()

    async def broadcast(msg):
        await hub.broadcast(msg)

    # Feed 容器（engine 需要先创建）
    engine_ref = {}

    async def on_cex_price(sym, bid, ask, ts):
        eng = engine_ref.get("engine")
        if eng:
            await eng.on_cex_price(sym, bid, ask, ts)

    cex_feed = BinanceWSFeed(on_cex_price)

    async def on_dex_price(sym, px, ts, src):
        eng = engine_ref.get("engine")
        if eng:
            await eng.on_dex_price(sym, px, ts, src)

    dex_feed = DexFeedManager(w3, on_dex_price, bnb_price_ref)

    engine = ArbEngine(
        cex_executor=cex_exec, dex_executor=dex_exec,
        dex_feed=dex_feed, cex_feed=cex_feed,
        bnb_price_ref=bnb_price_ref, ws_broadcaster=broadcast,
    )
    engine_ref["engine"] = engine

    # Scanner 回调：更新 candidates 到 engine
    async def on_scan_update(cands):
        await engine.on_candidates_update(cands)
        await broadcast({"type": "candidates_updated", "count": len(cands)})

    # 启动后台任务
    tasks = [
        asyncio.create_task(cex_feed.start(), name="cex_feed"),
        asyncio.create_task(dex_feed.start(), name="dex_feed"),
        asyncio.create_task(run_scanner_loop(w3, on_scan_update, bnb_price_ref), name="scanner"),
        asyncio.create_task(engine.run_unwind_loop(), name="unwind"),
    ]

    # 启动后立即订阅BNBUSDT（始终保持，作为WBNB价格参考）
    await cex_feed.update_subscriptions({"BNBUSDT"})

    # FastAPI
    app = build_app(engine_ref, hub)
    config = uvicorn.Config(
        app, host=STATIC.dashboard_host, port=STATIC.dashboard_port,
        log_level="info", access_log=False,
        proxy_headers=True,
        forwarded_allow_ips="*",  # 信任 Cloudflare Tunnel / localtunnel 等反代
        ws_ping_interval=20,
        ws_ping_timeout=20,
    )
    server = uvicorn.Server(config)

    server_task = asyncio.create_task(server.serve(), name="api")
    tasks.append(server_task)

    await DB.log_event("info", f"Dashboard: http://{STATIC.dashboard_host}:{STATIC.dashboard_port}")
    print(f"🚀 Dashboard: http://localhost:{STATIC.dashboard_port}")
    print(f"   DRY_RUN={RUNTIME.dry_run}  entry={RUNTIME.entry_threshold*100:.2f}%")

    # 优雅退出
    stop = asyncio.Event()

    def _stop(*_):
        print("\n⏹  Stopping...")
        stop.set()

    try:
        loop = asyncio.get_running_loop()
        loop.add_signal_handler(signal.SIGINT, _stop)
        loop.add_signal_handler(signal.SIGTERM, _stop)
    except NotImplementedError:
        pass  # Windows

    await stop.wait()

    # 先停 engine 接受新开仓
    RUNTIME.enabled = False
    await DB.log_event("info", "Shutdown requested; closing positions...")
    await engine.force_close_all("shutdown")

    await cex_feed.stop()
    await dex_feed.birdeye.stop()
    await cex_exec.close()
    server.should_exit = True

    for t in tasks:
        t.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)
    await DB.log_event("info", "Bot stopped")


if __name__ == "__main__":
    asyncio.run(amain())
