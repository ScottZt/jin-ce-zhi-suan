# DataProvider 独立封装包说明

## 简介
这是一个独立封装的中国A股市场数据获取模块，支持 **Tushare Pro** (推荐，需Token) 和 **Akshare** (开源免费) 两种数据源。
您可以直接将 `src/utils` 目录下的相关文件复制到您的其他项目中使用。

## 依赖库
使用前请确保安装以下 Python 库：
```bash
pip install pandas numpy tushare akshare requests
```

## 核心文件
1. `src/utils/data_factory.py`: 统一工厂入口
2. `src/utils/tushare_provider.py`: Tushare 数据源实现
3. `src/utils/akshare_provider.py`: Akshare 数据源实现
4. `src/utils/data_provider.py`: 默认/基类实现

## 快速上手

### 1. 引入模块
将 `src/utils` 文件夹复制到您的项目中。

### 2. 使用示例代码

```python
from src.utils.data_factory import DataFactory
from datetime import datetime, timedelta

# === 配置 ===
# 您的 Tushare Token (如果使用 Tushare)
MY_TOKEN = 'your_tushare_token_here' 

# === 初始化 ===
# 方式一：使用 Tushare (推荐，稳定)
factory = DataFactory(source='tushare', tushare_token=MY_TOKEN)

# 方式二：使用 Akshare (免费，无需Token，但可能不稳定)
# factory = DataFactory(source='akshare')

provider = factory.get_provider()

# === 获取历史分钟数据 ===
code = '600519.SH' # 贵州茅台
end_time = datetime.now()
start_time = end_time - timedelta(days=5)

print(f"正在获取 {code} 历史行情...")
df = provider.fetch_minute_data(code, start_time, end_time)

if not df.empty:
    print(f"成功获取 {len(df)} 条K线数据")
    print(df.head())
else:
    print("未获取到数据")

# === 获取最新实时行情 ===
print(f"正在获取 {code} 实时行情...")
tick = provider.get_latest_bar(code)

if tick:
    print(f"最新价: {tick['close']} (时间: {tick['dt']})")
else:
    print("获取实时行情失败")
```

## 接口说明

### `fetch_minute_data(code, start_time, end_time)`
获取指定时间段的历史分钟级 K 线数据。
- **参数**:
    - `code` (str): 股票代码 (如 '600036.SH')
    - `start_time` (datetime): 开始时间
    - `end_time` (datetime): 结束时间
- **返回**: `pandas.DataFrame` (包含 dt, open, high, low, close, vol, amount 列)

### `get_latest_bar(code)`
获取指定股票的最新一笔行情数据。
- **参数**:
    - `code` (str): 股票代码
- **返回**: `dict` (包含 dt, open, high, low, close, vol, amount 字段)
