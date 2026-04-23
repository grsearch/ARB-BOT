"""
全局配置。支持 .env 加载 + Dashboard 热更新。
"""
import os
from dataclasses import dataclass, asdict
from dotenv import load_dotenv

load_dotenv()


# ========== 链上常量（BSC Mainnet） ==========
BSC_CHAIN_ID = 56
WBNB = "0xbb4CdB9CBd36B01bD1cBaEBF2De08d9173bc095c"
USDT = "0x55d398326f99059fF775485246999027B3197955"
USDC = "0x8AC76a51cc950d9822D68b83fE1Ad97B32Cd580d"

PANCAKE_V3_FACTORY = "0x0BFbCF9fa4f9C56B0F40a671Ad40E0805A091865"
PANCAKE_V3_SWAP_ROUTER = "0x1b81D678ffb9C0263b24A97847620C99d213eB14"
PANCAKE_SMART_ROUTER = "0x13f4EA83D0bd40E75C8222255bc855a974568Dd4"

# PancakeSwap V2
PANCAKE_V2_FACTORY = "0xcA143Ce32Fe78f1f7019d7d551a6402fC5350c73"
PANCAKE_V2_ROUTER  = "0x10ED43C718714eb63d5aA57B78B54704E256024E"
V2_SWAP_FEE_BPS = 25   # PancakeSwap V2 固定 0.25% 手续费

V3_FEE_TIERS = [100, 500, 2500, 10000]   # 0.01%, 0.05%, 0.25%, 1%


# ========== 币安常量 ==========
BINANCE_SPOT_REST = "https://api.binance.com"
BINANCE_FUTURES_REST = "https://fapi.binance.com"
BINANCE_FUTURES_WS = "wss://fstream.binance.com/stream"

# 币安USDT-M合约手续费（taker市价单）
BINANCE_TAKER_FEE = 0.00045    # 0.045%（用户确认，已含BNB抵扣）
BINANCE_MAKER_FEE = 0.00018


# ========== 价格数据源 ==========
BIRDEYE_WS_BSC = "wss://public-api.birdeye.so/socket/bsc"
BIRDEYE_REST = "https://public-api.birdeye.so"
GECKO_TERMINAL_REST = "https://api.geckoterminal.com/api/v2"


# ========== Gas 估算 ==========
TYPICAL_SWAP_GAS_UNITS = 180_000


@dataclass
class RuntimeConfig:
    """运行时参数，可从Dashboard动态修改"""
    entry_threshold: float = 0.025
    exit_threshold: float = 0.005
    position_usdt: float = 500.0
    max_slippage: float = 0.012
    leverage: int = 3
    max_concurrent_positions: int = 2
    max_exec_latency_ms: int = 2000
    dry_run: bool = True
    # 扫描参数（基于实战调优）
    scan_interval_sec: int = 120            # 2分钟一次（原15分钟太慢）
    top_n_gainers: int = 30                 # Top 30（原10个经常找不到BSC币）
    min_24h_gain_pct: float = 0.02          # 2%（原5%太严）
    min_pool_tvl_usd: float = 10_000        # $10k（原$100k过滤掉太多新币；新池子链上TVL可能很低）
    max_pool_fee_bps: int = 10000           # 允许到 1% fee 池（原2500过滤掉SPK这种1%池）
    enabled: bool = True
    # 手续费
    cex_taker_fee: float = BINANCE_TAKER_FEE
    cex_maker_fee: float = BINANCE_MAKER_FEE
    # Gas: 对基础 gas_price 的乘数（1.0=不加价，2.0=2倍，3.0=3倍抢速度）
    # BSC 通常 1 Gwei，2倍=2 Gwei，让验证者优先打包
    gas_boost_multiplier: float = 1.5
    # 黑名单：逗号分隔的 symbol 列表，扫描+开仓双重拦截
    # 例如貔貅盘 ONUSDT、卖出限制的币
    symbol_blacklist: str = ""
    # BNB 最低余额保护 (低于此值停止发送 DEX TX，避免gas耗尽)
    min_bnb_balance: float = 0.002

    def to_dict(self):
        return asdict(self)

    def to_dict(self):
        return asdict(self)


@dataclass
class StaticConfig:
    binance_api_key: str = ""
    binance_api_secret: str = ""
    bsc_rpc_http: str = ""
    bsc_rpc_ws: str = ""
    bsc_rpc_backup: str = "https://bsc-dataseed.binance.org"
    wallet_private_key: str = ""
    wallet_address: str = ""
    birdeye_api_key: str = ""
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    dashboard_port: int = 8000
    dashboard_host: str = "0.0.0.0"


def load_static_config() -> StaticConfig:
    return StaticConfig(
        binance_api_key=os.getenv("BINANCE_API_KEY", ""),
        binance_api_secret=os.getenv("BINANCE_API_SECRET", ""),
        bsc_rpc_http=os.getenv("BSC_RPC_HTTP", ""),
        bsc_rpc_ws=os.getenv("BSC_RPC_WS", ""),
        bsc_rpc_backup=os.getenv("BSC_RPC_BACKUP", "https://bsc-dataseed.binance.org"),
        wallet_private_key=os.getenv("WALLET_PRIVATE_KEY", ""),
        wallet_address=os.getenv("WALLET_ADDRESS", ""),
        birdeye_api_key=os.getenv("BIRDEYE_API_KEY", ""),
        telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID", ""),
        dashboard_port=int(os.getenv("DASHBOARD_PORT", "8000")),
        dashboard_host=os.getenv("DASHBOARD_HOST", "0.0.0.0"),
    )


def load_runtime_config() -> RuntimeConfig:
    def _f(key: str, default: float) -> float:
        try: return float(os.getenv(key, default))
        except: return default
    def _i(key: str, default: int) -> int:
        try: return int(os.getenv(key, default))
        except: return default
    def _b(key: str, default: bool) -> bool:
        v = os.getenv(key, str(default)).strip().lower()
        return v in ("1", "true", "yes", "y")

    # 优先级：DB持久化 > .env > 默认值
    # DB 里是 Dashboard 改的值（用户最新意愿），重启后保留
    try:
        from .db import DB
        db_overrides = DB.load_runtime_overrides_sync()
    except Exception:
        db_overrides = {}

    rc = RuntimeConfig(
        entry_threshold=_f("ENTRY_THRESHOLD", 0.025),
        exit_threshold=_f("EXIT_THRESHOLD", 0.005),
        position_usdt=_f("POSITION_USDT", 500.0),
        max_slippage=_f("MAX_SLIPPAGE", 0.012),
        leverage=_i("LEVERAGE", 3),
        max_concurrent_positions=_i("MAX_CONCURRENT_POSITIONS", 2),
        max_exec_latency_ms=_i("MAX_EXEC_LATENCY_MS", 2000),
        dry_run=_b("DRY_RUN", True),
        scan_interval_sec=_i("SCAN_INTERVAL_SEC", 120),
        top_n_gainers=_i("TOP_N_GAINERS", 30),
        min_24h_gain_pct=_f("MIN_24H_GAIN_PCT", 0.02),
        min_pool_tvl_usd=_f("MIN_POOL_TVL_USD", 10_000),
        max_pool_fee_bps=_i("MAX_POOL_FEE_BPS", 10000),
        enabled=_b("ENABLED", True),
        cex_taker_fee=_f("CEX_TAKER_FEE", BINANCE_TAKER_FEE),
        cex_maker_fee=_f("CEX_MAKER_FEE", BINANCE_MAKER_FEE),
        gas_boost_multiplier=_f("GAS_BOOST_MULTIPLIER", 1.5),
        symbol_blacklist=os.getenv("SYMBOL_BLACKLIST", "").strip(),
        min_bnb_balance=_f("MIN_BNB_BALANCE", 0.002),
    )

    # DB 覆盖（用户从 Dashboard 改的值，优先级最高）
    for k, v in db_overrides.items():
        if hasattr(rc, k):
            setattr(rc, k, v)

    return rc


STATIC = load_static_config()
RUNTIME = load_runtime_config()
