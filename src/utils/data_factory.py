from src.utils.tushare_provider import TushareProvider
from src.utils.akshare_provider import AkshareProvider
from src.utils.data_provider import DataProvider
import pandas as pd
import tushare as ts

class DataFactory:
    """
    统一数据源工厂类 (DataFactory)
    
    使用说明:
    1. 实例化 DataFactory，指定数据源类型 ('tushare', 'akshare', 'default')。
    2. 如果使用 tushare，需要提供 token。
    3. 调用 get_provider() 获取具体的 Provider 实例。
    4. 使用 fetch_minute_data() 或 get_latest_bar() 获取数据。
    
    示例:
    factory = DataFactory(source='tushare', tushare_token='YOUR_TOKEN')
    provider = factory.get_provider()
    df = provider.fetch_minute_data('000001.SZ', start_date, end_date)
    """
    
    def __init__(self, source='akshare', tushare_token=None):
        self.source = source
        self.tushare_token = tushare_token
        self.provider = self._create_provider()
        
    def _create_provider(self):
        if self.source == 'tushare':
            if not self.tushare_token:
                # Try to load from env or config if not provided? 
                # For encapsulation, better to fail or warn.
                print("Warning: Tushare source selected but no token provided.")
            return TushareProvider(token=self.tushare_token)
            
        elif self.source == 'akshare':
            return AkshareProvider()
            
        else:
            return DataProvider()
            
    def get_provider(self):
        return self.provider

# 导出工具类，方便外部直接 import 使用
__all__ = ['DataFactory', 'TushareProvider', 'AkshareProvider', 'DataProvider']
