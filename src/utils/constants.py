# src/utils/constants.py

# 交易成本
COMMISSION_RATE = 0.00025  # 万2.5
MIN_COMMISSION = 5.0       # 最低5元
STAMP_DUTY = 0.001         # 印花税 0.1% (卖出)
TRANSFER_FEE = 0.00001     # 过户费 万0.1
SLIPPAGE = 0.001           # 滑点 0.1%

# 风控红线
MAX_STOP_LOSS_PCT = 0.05       # 单笔止损 <= 5%
MAX_POS_PER_STOCK = 0.10       # 单票最大仓位 10%
MAX_TOTAL_POS = 0.50           # 总仓位上限 50%
MAX_DAILY_LOSS_PCT = 0.02      # 单日最大亏损 2%
MAX_DRAWDOWN_TRIGGER = 0.10    # 最大回撤达 10% 触发降频
CONSECUTIVE_LOSS_LIMIT = 3     # 连续亏损3笔当日停止开仓

# 其它
INITIAL_CAPITAL = 1000000.0    # 初始资金 100万 (根据最新要求修改)
RISK_FREE_RATE = 0.02          # 无风险利率 2%
MIN_LIQUIDITY = 100000000.0    # 日均成交额 < 1亿 禁止参与
