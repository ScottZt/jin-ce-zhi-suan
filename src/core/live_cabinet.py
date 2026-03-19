# src/core/live_cabinet.py
import time
import pandas as pd
from datetime import datetime, timedelta
from src.utils.data_provider import DataProvider
from src.core.crown_prince import CrownPrince
from src.core.zhongshu_sheng import ZhongshuSheng
from src.core.menxia_sheng import MenxiaSheng
from src.core.shangshu_sheng import ShangshuSheng
from src.ministries.li_bu_personnel import LiBuPersonnel
from src.ministries.hu_bu_revenue import HuBuRevenue
from src.ministries.li_bu_rites import LiBuRites
from src.ministries.bing_bu_war import BingBuWar
from src.ministries.xing_bu_justice import XingBuJustice
from src.ministries.gong_bu_works import GongBuWorks
import src.strategies.strategy_factory as strategy_factory_module
from src.utils.data_provider import DataProvider
import asyncio
from src.utils.tushare_provider import TushareProvider
from src.utils.akshare_provider import AkshareProvider
from src.utils.indicators import Indicators

class LiveCabinet:
    def __init__(self, stock_code, initial_capital=1000000.0, provider_type='default', tushare_token=None, event_callback=None):
        self.stock_code = stock_code
        self.event_callback = event_callback # Callback for UI updates
        
        if provider_type == 'tushare':
            self.provider = TushareProvider(token=tushare_token)
            print("🌐 Data Source: Tushare Pro")
        elif provider_type == 'akshare':
            self.provider = AkshareProvider()
            print("🌐 Data Source: Akshare (Free)")
        else:
            self.provider = DataProvider()
            print("🌐 Data Source: Default API")
        
        # Initialize Ministries
        self.personnel = LiBuPersonnel()
        self.revenue = HuBuRevenue(initial_capital)
        self.rites = LiBuRites()
        self.war = BingBuWar()
        self.justice = XingBuJustice()
        self.works = GongBuWorks()

        # Initialize Departments
        self.prince = CrownPrince()
        self.chancellery = MenxiaSheng(self.justice)
        self.state_affairs = ShangshuSheng(self.revenue, self.war, self.justice)

        # Initialize Strategies
        self.strategies = strategy_factory_module.create_strategies()
        self.all_strategies = self.strategies
        self.secretariat = ZhongshuSheng(self.strategies)
        self.active_strategy_ids = [s.id for s in self.strategies] # Default all active
        self.strategy_trigger_tf = {s.id: getattr(s, "trigger_timeframe", "1min") for s in self.strategies}
        
        for s in self.strategies:
            self.personnel.register_strategy(s)
            
        self.last_dt = None
        self.daily_data_buffer = [] # Buffer for daily data
        self.today_archived = False

    def warm_up(self):
        """
        Load historical data to initialize strategy indicators.
        """
        print(f"🔥 正在预热策略内阁 (获取 {self.stock_code} 历史数据)...")
        
        # Get latest time first to know where to look back from
        latest = self.provider.get_latest_bar(self.stock_code)
        if not latest:
            print("❌ 无法获取最新行情，预热失败。")
            return False
            
        end_time = latest['dt']
        # Look back 5 days to ensure indicators have enough data (Speed optimization for demo)
        start_time = end_time - timedelta(days=5) 
        
        df = self.provider.fetch_minute_data(self.stock_code, start_time, end_time)
        if df.empty:
            print("❌ 历史数据为空，策略无法初始化。")
            return False
            
        df = self.works.clean_data(df)
        print(f"✅ 获取到 {len(df)} 条历史K线，正在回放以初始化策略状态...")
        
        # Bulk update history for all strategies to speed up warm-up
        for strategy in self.strategies:
            # Initialize history with the full dataframe
            strategy.history[self.stock_code] = df.copy()
            # Run on_bar for the last few bars to ensure indicators are primed if they use internal state
            # (Though our current strategies mostly recalc from history df)
            # Actually, just letting them have the history is enough for the next 'real' bar.
            # But let's run the last one to be sure nothing crashes.
            strategy.on_bar(df.iloc[-1])
            
        self.last_dt = df.iloc[-1]['dt']
        print(f"✅ 预热完成。最新数据时间: {self.last_dt}")
        return True

    async def _emit_event(self, event_type, data):
        """
        Emit event to frontend via callback (async).
        """
        if self.event_callback:
            await self.event_callback(event_type, data)

    async def run_live(self):
        """
        Start the live monitoring loop (Async version).
        """
        # Emit strategy list first
        strategy_list = [
            {"id": s.id, "name": s.name} for s in self.strategies
        ]
        await self._emit_event('init_strategies', strategy_list)
        
        # Emit initial account status
        await self._emit_event('account', {
            'assets': self.revenue.cash,
            'pnl': "0.00%",
            'pos_ratio': "0%" 
        })
        
        # Try warm up, but if it fails (e.g. no connection), we can still run in simulation mode for demo
        warmup_success = self.warm_up()
        
        print(f"\n🚀 【三省六部】实时监控已启动")
        print(f"👁️  监控标的: {self.stock_code}")
        print(f"🛡️  运行策略: {len(self.strategies)} 套")
        if not warmup_success:
            print("⚠️ 真实数据预热失败，将使用【模拟数据模式】进行演示...")
        print("--------------------------------------------------")
        
        # Initial Event
        await self._emit_event('system', {'msg': '内阁实时监控已启动', 'stock': self.stock_code})
        
        import asyncio
        try:
            while True:
                await self._tick(simulation_mode=not warmup_success)
                await asyncio.sleep(3) # Faster tick for demo (3s)
        except asyncio.CancelledError:
            print("\n🛑 实时监控任务已取消。")
        except KeyboardInterrupt:
            print("\n🛑 实时监控已停止。")

    def set_active_strategies(self, strategy_id):
        """
        Filter active strategies.
        If strategy_id is 'all', enable all.
        Otherwise, only enable the specified ID.
        """
        if isinstance(strategy_id, list):
            picked = set([str(x) for x in strategy_id if str(x)])
            if not picked:
                self.active_strategy_ids = [s.id for s in self.strategies]
            else:
                self.active_strategy_ids = [s.id for s in self.strategies if s.id in picked]
        elif strategy_id == 'all':
            self.active_strategy_ids = [s.id for s in self.strategies]
        else:
            self.active_strategy_ids = [strategy_id]
        print(f"🔄 策略池已更新: {self.active_strategy_ids}")

    def _is_timeframe_tick(self, dt, timeframe):
        if timeframe == "1min":
            return True
        minute = int(dt.minute)
        hour = int(dt.hour)
        if timeframe == "5min":
            return minute % 5 == 0
        if timeframe == "10min":
            return minute % 10 == 0
        if timeframe == "15min":
            return minute % 15 == 0
        if timeframe == "30min":
            return minute % 30 == 0
        if timeframe == "60min":
            return minute == 0
        if timeframe == "D":
            return hour >= 15 and minute == 0
        return True

    def _get_runnable_strategy_ids(self, current_dt):
        runnable = []
        for sid in self.active_strategy_ids:
            if sid is None:
                continue
            tf = self.strategy_trigger_tf.get(sid, "1min")
            if self._is_timeframe_tick(current_dt, tf):
                runnable.append(sid)
        return runnable

    def _format_tick_trigger_log(self, runnable_strategy_ids):
        grouped = {}
        for sid in runnable_strategy_ids:
            if sid is None:
                continue
            tf = self.strategy_trigger_tf.get(sid, "1min")
            grouped.setdefault(tf, []).append(str(sid))
        tf_order = ["1min", "5min", "10min", "15min", "30min", "60min", "D"]
        parts = []
        for tf in tf_order:
            ids = grouped.get(tf, [])
            if ids:
                parts.append(f"{tf}:{'/'.join(ids)}")
        names = []
        for sid in runnable_strategy_ids:
            strategy = self.personnel.get_strategy(sid)
            if strategy:
                names.append(f"{sid}-{strategy.name}")
            else:
                names.append(sid)
        return " | ".join(parts), "、".join(names)

    async def _tick(self, simulation_mode=False):
        current_dt = None
        bar = None
        api_latency_ms = 0
        
        if simulation_mode:
            # Generate Mock Bar
            import random
            if not self.last_dt:
                self.last_dt = datetime.now()
                self.current_price = 15.0 # Mock start price
            else:
                self.last_dt = self.last_dt + timedelta(minutes=1)
                
            change_pct = (random.random() - 0.5) * 0.02
            self.current_price = self.current_price * (1 + change_pct)
            
            bar = {
                'code': self.stock_code,
                'dt': self.last_dt,
                'open': self.current_price,
                'high': self.current_price * 1.005,
                'low': self.current_price * 0.995,
                'close': self.current_price,
                'vol': random.randint(1000, 10000),
                'amount': random.randint(10000, 100000)
            }
            current_dt = self.last_dt
        else:
            # 1. Get Real-time Data
            t0 = time.perf_counter()
            bar = self.provider.get_latest_bar(self.stock_code)
            api_latency_ms = int((time.perf_counter() - t0) * 1000)
            if not bar:
                return

            current_dt = bar['dt']
            
            # Only process if we have a NEW bar (timestamp changed)
            if self.last_dt and current_dt <= self.last_dt:
                # Still the same bar, wait for update
                print(f"⏳ 等待K线更新... (当前: {current_dt})", end='\r')
                return
            self.last_dt = current_dt
        if "volume" not in bar and "vol" in bar:
            bar["volume"] = bar["vol"]
        if "vol" not in bar and "volume" in bar:
            bar["vol"] = bar["volume"]
        if "turnover" not in bar and "amount" in bar:
            bar["turnover"] = bar["amount"]
        if "amount" not in bar and "turnover" in bar:
            bar["amount"] = bar["turnover"]

        self.daily_data_buffer.append(bar)
        hist_df = pd.DataFrame(self.daily_data_buffer)
        close_series = pd.to_numeric(hist_df.get("close"), errors="coerce").ffill().bfill()
        ma5 = float(Indicators.MA(close_series, 5).fillna(close_series).iloc[-1]) if len(close_series) else float(bar["close"])
        _, _, macd_series = Indicators.MACD(close_series) if len(close_series) else (None, None, pd.Series([0.0]))
        rsi_series = Indicators.RSI(close_series) if len(close_series) else pd.Series([50.0])
        macd_val = float(pd.Series(macd_series).fillna(0.0).iloc[-1]) if len(close_series) else 0.0
        rsi_val = float(pd.Series(rsi_series).fillna(50.0).iloc[-1]) if len(close_series) else 50.0

        # Emit Market Data Event
        await self._emit_event('market', {
            'price': f"{bar['close']:.2f}",
            'ma5': f"{ma5:.2f}",
            'macd': float(macd_val),
            'rsi': float(rsi_val),
            'time': current_dt.strftime("%H:%M:%S"),
            'kline_timeframe': '1分钟线',
            'kline_dt': str(current_dt)
        })
        holdings_value_now = self.state_affairs.update_holdings_value({bar['code']: bar['close']})
        fund_value_now = float(self.revenue.cash) + float(holdings_value_now)
        pos_ratio_now = (holdings_value_now / fund_value_now * 100.0) if fund_value_now > 0 else 0.0
        await self._emit_event('account', {
            'assets': round(fund_value_now, 2),
            'cash': round(float(self.revenue.cash), 2),
            'pnl': "0.00%",
            'pos_ratio': f"{pos_ratio_now:.2f}%"
        })

        print(f"\n🆕 新K线生成: {current_dt} | Close: {bar['close']:.2f}")
        
        # Check Archive
        self._check_market_close(current_dt)
        
        runnable_strategy_ids = self._get_runnable_strategy_ids(current_dt)
        tf_text, name_text = self._format_tick_trigger_log(runnable_strategy_ids)
        print(f"   ⏱ 本tick触发周期与策略: {tf_text if tf_text else '无'}")
        print(f"   📋 本tick策略列表: {name_text if name_text else '无'}")
        signals = self.secretariat.generate_signals(
            bar,
            runnable_strategy_ids=runnable_strategy_ids,
            strategy_context={
                "current_cash": float(self.revenue.cash),
                "last_price": float(bar.get("close", 0.0))
            }
        )
        active_signals = signals
        buy_signals = len([s for s in active_signals if s.get('direction') == 'BUY'])
        sell_signals = len([s for s in active_signals if s.get('direction') == 'SELL'])
        if len(active_signals) == 0:
            signal_consistency = 50.0
        else:
            signal_consistency = max(buy_signals, sell_signals) / len(active_signals) * 100.0
        await self._emit_event('live_tick', {
            'time': current_dt.strftime("%H:%M:%S"),
            'trigger_summary': tf_text if tf_text else '无',
            'strategy_summary': name_text if name_text else '无',
            'runnable_ids': runnable_strategy_ids,
            'runnable_count': len(runnable_strategy_ids),
            'signal_count': len(active_signals),
            'signal_consistency': round(signal_consistency, 2),
            'api_latency_ms': api_latency_ms
        })
        
        if not active_signals:
            print("   💤 无策略信号")
        
        for signal in active_signals:
            strategy_id = signal['strategy_id']
            strategy_name = self.personnel.get_strategy(strategy_id).name
            direction = "🟢 买入" if signal['direction'] == 'BUY' else "🔴 卖出"
            
            # Emit Signal Event
            formatted_price = f"{signal['price']:.2f}"
            
            await self._emit_event('zhongshu', {
                'msg': f"策略 [{strategy_name}] 生成信号: {direction} @ {formatted_price}",
                'details': f"> 策略: {strategy_name}<br>> 方向: {direction}<br>> 价格: {formatted_price}",
                'status': 'bg-trading-blue'
            })
            
            print(f"   💡 信号触发! [{strategy_name}] 请求 {direction} {signal['qty']}股 @ {formatted_price}")
            
            # 3. Risk Control (MenxiaSheng)
            # Need approximate portfolio value. 
            # In live mode, we might just use initial capital for calc or track virtual position.
            current_fund_value = self.revenue.cash + self.state_affairs.update_holdings_value({bar['code']: bar['close']})
            current_positions = self.state_affairs.positions.get(strategy_id, {})
            
            approved, reason = self.chancellery.check_signal(signal, current_fund_value, current_positions, 0.0)
            
            if approved:
                print(f"   ✅ 风控中心批准。正在执行...")
                await self._emit_event('menxia', {
                    'msg': "风控审核通过",
                    'details': "> 风险敞口: 合规<br>> 批准执行",
                    'status': 'bg-trading-green',
                    'decision': 'approved',
                    'strategy_id': strategy_id
                })
                
                # 4. Execution (ShangshuSheng) - Virtual Execution
                await self._emit_event('shangshu', {
                    'msg': "正在下达交易指令...",
                    'details': f"> 目标: {self.stock_code}<br>> 动作: {direction}",
                    'status': 'bg-trading-yellow'
                })
                
                executed = self.state_affairs.execute_order(strategy_id, signal, bar)
                if executed:
                    new_qty = self.state_affairs.positions[strategy_id][signal['code']]['qty'] if signal['code'] in self.state_affairs.positions.get(strategy_id, {}) else 0
                    self.secretariat.update_strategy_state(strategy_id, signal['code'], new_qty)
                    print(f"   🚀 执行成功! 当前持仓: {new_qty}")
                    realized_pnl = None
                    if self.revenue.transactions:
                        last_tx = self.revenue.transactions[-1]
                        if last_tx.get('strategy_id') == strategy_id and last_tx.get('direction') == 'SELL':
                            realized_pnl = float(last_tx.get('pnl', 0.0))
                    await self._emit_event('trade_exec', {
                        'time': current_dt.strftime("%H:%M:%S"),
                        'strategy_id': strategy_id,
                        'direction': signal.get('direction'),
                        'price': float(signal.get('price', 0.0)),
                        'qty': int(signal.get('qty', 0)),
                        'realized_pnl': realized_pnl
                    })
                    
                    await self._emit_event('shangshu', {
                    'msg': f"执行成功! 持仓: {new_qty}",
                    'details': f"> 成交价: {bar['close']:.2f}<br>> 新持仓: {new_qty}",
                    'status': 'bg-trading-green'
                })
                    
                    # Update Account UI
                    await self._emit_event('account', {
                        'assets': current_fund_value,
                        'pnl': "0.00%" # TODO: Calc PnL
                    })
            else:
                print(f"   ❌ 风控中心驳回: {reason}")
                await self._emit_event('menxia', {
                    'msg': f"风控驳回: {reason}",
                    'details': f"> 原因: {reason}<br>> 建议: 观望",
                    'status': 'bg-trading-red',
                    'decision': 'rejected',
                    'strategy_id': strategy_id,
                    'reason': reason
                })

        # 5. Check Stops
        triggered_stops = self.state_affairs.check_stops(bar)
        for order in triggered_stops:
            print(f"   ⚡ 触发风控止盈/止损: {order['type']} @ {order['price']:.2f}")
            await self._emit_event('menxia', {
                    'msg': f"触发风控: {order['type']}",
                    'details': f"> 类型: {order['type']}<br>> 价格: {order['price']:.2f}",
                    'status': 'bg-trading-red'
                })
            self.state_affairs.execute_order(order['strategy_id'], order, bar)
            self.secretariat.update_strategy_state(order['strategy_id'], order['code'], 0)
            realized_pnl = None
            if self.revenue.transactions:
                last_tx = self.revenue.transactions[-1]
                if last_tx.get('strategy_id') == order.get('strategy_id') and last_tx.get('direction') == 'SELL':
                    realized_pnl = float(last_tx.get('pnl', 0.0))
            await self._emit_event('trade_exec', {
                'time': current_dt.strftime("%H:%M:%S"),
                'strategy_id': order.get('strategy_id'),
                'direction': 'SELL',
                'price': float(order.get('price', 0.0)),
                'qty': int(order.get('qty', 0)),
                'realized_pnl': realized_pnl
            })

    def _check_market_close(self, current_dt):
        # Archive at 15:00
        if current_dt.hour >= 15 and not self.today_archived:
            print(f"🌆 收盘时间到 ({current_dt})，正在归档今日数据...")
            self._archive_data()
            self.today_archived = True
            
        # Reset archived flag next day (simplified check)
        if current_dt.hour < 9:
            self.today_archived = False

    def _archive_data(self):
        if not self.daily_data_buffer:
            print("⚠️ 今日无数据需归档。")
            return

        import os
        import csv
        
        # 1. Save to Local CSV
        filename = f"data/history/{self.stock_code}.csv"
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        
        file_exists = os.path.isfile(filename)
        
        try:
            with open(filename, 'a', newline='') as f:
                writer = csv.DictWriter(f, fieldnames=['code', 'dt', 'open', 'high', 'low', 'close', 'vol', 'amount'])
                if not file_exists:
                    writer.writeheader()
                
                # Format dt to string if needed, but csv module handles datetime object usually or str()
                for row in self.daily_data_buffer:
                     writer.writerow(row)
                     
            print(f"✅ 已保存 {len(self.daily_data_buffer)} 条数据到本地: {filename}")
        except Exception as e:
            print(f"❌ 本地归档失败: {e}")

        # 2. Push to Remote API (Placeholder)
        # self.provider.push_data_to_remote(self.daily_data_buffer)
        
        # Clear Buffer
        self.daily_data_buffer = []
