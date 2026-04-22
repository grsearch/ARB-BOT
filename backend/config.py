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
    # 扫描参数
    scan_interval_sec: int = 900
    top_n_gainers: int = 10
    min_24h_gain_pct: float = 0.05
    min_pool_tvl_usd: float = 100_000
    max_pool_fee_bps: int = 2500        # 最大 0.25% 池子 fee，1% 的池子不碰
    enabled: bool = True
    # 手续费
    cex_taker_fee: float = BINANCE_TAKER_FEE
    cex_maker_fee: float = BINANCE_MAKER_FEE

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

    return RuntimeConfig(
        entry_threshold=_f("ENTRY_THRESHOLD", 0.025),
        exit_threshold=_f("EXIT_THRESHOLD", 0.005),
        position_usdt=_f("POSITION_USDT", 500.0),
        max_slippage=_f("MAX_SLIPPAGE", 0.012),
        leverage=_i("LEVERAGE", 3),
        max_concurrent_positions=_i("MAX_CONCURRENT_POSITIONS", 2),
        max_exec_latency_ms=_i("MAX_EXEC_LATENCY_MS", 2000),
        dry_run=_b("DRY_RUN", True),
    )


STATIC = load_static_config()
RUNTIME = load_runtime_config()
