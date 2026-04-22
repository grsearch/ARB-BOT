"""
精简版ABI，只保留我们会用到的函数，签到/编码更快。
"""

ERC20_ABI = [
    {"name":"balanceOf","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"}],"outputs":[{"name":"","type":"uint256"}]},
    {"name":"allowance","type":"function","stateMutability":"view",
     "inputs":[{"name":"","type":"address"},{"name":"","type":"address"}],
     "outputs":[{"name":"","type":"uint256"}]},
    {"name":"approve","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"spender","type":"address"},{"name":"amount","type":"uint256"}],
     "outputs":[{"name":"","type":"bool"}]},
    {"name":"decimals","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint8"}]},
    {"name":"symbol","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"string"}]},
]

# PancakeSwap V3 Factory - getPool
V3_FACTORY_ABI = [
    {"name":"getPool","type":"function","stateMutability":"view",
     "inputs":[{"name":"tokenA","type":"address"},
               {"name":"tokenB","type":"address"},
               {"name":"fee","type":"uint24"}],
     "outputs":[{"name":"pool","type":"address"}]},
]

# PancakeSwap V3 Pool - slot0 + liquidity + token0/token1
V3_POOL_ABI = [
    {"name":"slot0","type":"function","stateMutability":"view","inputs":[],
     "outputs":[
        {"name":"sqrtPriceX96","type":"uint160"},
        {"name":"tick","type":"int24"},
        {"name":"observationIndex","type":"uint16"},
        {"name":"observationCardinality","type":"uint16"},
        {"name":"observationCardinalityNext","type":"uint16"},
        {"name":"feeProtocol","type":"uint32"},
        {"name":"unlocked","type":"bool"},
     ]},
    {"name":"liquidity","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint128"}]},
    {"name":"token0","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"address"}]},
    {"name":"token1","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"address"}]},
    {"name":"fee","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"uint24"}]},
]

# PancakeSwap V3 SwapRouter - exactInputSingle
V3_SWAP_ROUTER_ABI = [
    {"name":"exactInputSingle","type":"function","stateMutability":"payable",
     "inputs":[{"name":"params","type":"tuple","components":[
        {"name":"tokenIn","type":"address"},
        {"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},
        {"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMinimum","type":"uint256"},
        {"name":"sqrtPriceLimitX96","type":"uint160"},
     ]}],
     "outputs":[{"name":"amountOut","type":"uint256"}]},
    {"name":"exactOutputSingle","type":"function","stateMutability":"payable",
     "inputs":[{"name":"params","type":"tuple","components":[
        {"name":"tokenIn","type":"address"},
        {"name":"tokenOut","type":"address"},
        {"name":"fee","type":"uint24"},
        {"name":"recipient","type":"address"},
        {"name":"deadline","type":"uint256"},
        {"name":"amountOut","type":"uint256"},
        {"name":"amountInMaximum","type":"uint256"},
        {"name":"sqrtPriceLimitX96","type":"uint160"},
     ]}],
     "outputs":[{"name":"amountIn","type":"uint256"}]},
]

# Quoter V2 - 用于预估输出，支持 view 调用，不真实下单
# https://bscscan.com/address/0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997
PANCAKE_V3_QUOTER = "0xB048Bbc1Ee6b733FFfCFb9e9CeF7375518e25997"
V3_QUOTER_ABI = [
    {"name":"quoteExactInputSingle","type":"function","stateMutability":"nonpayable",
     "inputs":[{"name":"params","type":"tuple","components":[
        {"name":"tokenIn","type":"address"},
        {"name":"tokenOut","type":"address"},
        {"name":"amountIn","type":"uint256"},
        {"name":"fee","type":"uint24"},
        {"name":"sqrtPriceLimitX96","type":"uint160"},
     ]}],
     "outputs":[
        {"name":"amountOut","type":"uint256"},
        {"name":"sqrtPriceX96After","type":"uint160"},
        {"name":"initializedTicksCrossed","type":"uint32"},
        {"name":"gasEstimate","type":"uint256"},
     ]},
]

# -------- PancakeSwap V2 --------
V2_FACTORY_ABI = [
    {"name":"getPair","type":"function","stateMutability":"view",
     "inputs":[{"name":"tokenA","type":"address"},
               {"name":"tokenB","type":"address"}],
     "outputs":[{"name":"pair","type":"address"}]},
]

V2_PAIR_ABI = [
    {"name":"getReserves","type":"function","stateMutability":"view","inputs":[],
     "outputs":[
        {"name":"_reserve0","type":"uint112"},
        {"name":"_reserve1","type":"uint112"},
        {"name":"_blockTimestampLast","type":"uint32"},
     ]},
    {"name":"token0","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"address"}]},
    {"name":"token1","type":"function","stateMutability":"view",
     "inputs":[],"outputs":[{"name":"","type":"address"}]},
]

V2_ROUTER_ABI = [
    {"name":"swapExactTokensForTokensSupportingFeeOnTransferTokens",
     "type":"function","stateMutability":"nonpayable",
     "inputs":[
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMin","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"},
     ],
     "outputs":[]},
    {"name":"swapExactTokensForTokens","type":"function","stateMutability":"nonpayable",
     "inputs":[
        {"name":"amountIn","type":"uint256"},
        {"name":"amountOutMin","type":"uint256"},
        {"name":"path","type":"address[]"},
        {"name":"to","type":"address"},
        {"name":"deadline","type":"uint256"},
     ],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
    {"name":"getAmountsOut","type":"function","stateMutability":"view",
     "inputs":[
        {"name":"amountIn","type":"uint256"},
        {"name":"path","type":"address[]"},
     ],
     "outputs":[{"name":"amounts","type":"uint256[]"}]},
]
