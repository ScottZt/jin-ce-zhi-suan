
import asyncio
import pandas as pd
from datetime import datetime, timedelta
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
from src.strategies.strategy_factory import create_strategies
from src.utils.data_provider import DataProvider
from src.utils.tushare_provider import TushareProvider
from src.utils.akshare_provider import AkshareProvider
from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators

class BacktestCabinet:
    def __init__(self, stock_code, strategy_id='all', initial_capital=1000000.0, event_callback=None, strategy_mode=None, strategy_ids=None):
        self.stock_code = stock_code
        self.strategy_id = strategy_id
        self.initial_capital = initial_capital
        self.event_callback = event_callback
        self.strategy_mode = strategy_mode
        self.strategy_ids = strategy_ids
        self.config = ConfigLoader.reload()
        
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
        # Get the latest strategies every time we start a backtest
        all_strategies = create_strategies()
        if self.strategy_ids:
            allowed = set(self.strategy_ids)
            self.strategies = [s for s in all_strategies if s.id in allowed]
        elif self.strategy_mode == 'top5':
            self.strategies = all_strategies[:5]
        elif self.strategy_id == 'all':
            self.strategies = all_strategies
        else:
            self.strategies = [s for s in all_strategies if s.id == self.strategy_id]
            
        self.secretariat = ZhongshuSheng(self.strategies)
        strategy_count = max(1, len(self.strategies))
        self.strategy_initial_capital = self.initial_capital / strategy_count
        self.strategy_revenues = {s.id: HuBuRevenue(self.strategy_initial_capital) for s in self.strategies}
        self.aggregate_nav = []
        
        for s in self.strategies:
            self.personnel.register_strategy(s)

    async def _emit(self, event_type, data):
        if self.event_callback:
            await self.event_callback(event_type, data)

    async def _emit_account_snapshot(self, kline, active_strategy_id=None, compliance_status="PASS"):
        current_prices = {kline['code']: kline['close']}
        holdings_value = 0.0
        cash_total = 0.0
        for sid, account in self.strategy_revenues.items():
            strategy_holdings = self.state_affairs.update_strategy_holdings_value(sid, current_prices)
            account.update_daily_nav(kline['dt'], strategy_holdings)
            holdings_value += strategy_holdings
            cash_total += float(account.cash)
        fund_value = cash_total + holdings_value
        pnl_pct = ((fund_value / self.initial_capital) - 1.0) * 100 if self.initial_capital else 0.0
        pos_ratio = (holdings_value / fund_value * 100) if fund_value > 0 else 0.0
        await self._emit('account', {
            'assets': round(fund_value, 2),
            'cash': round(cash_total, 2),
            'pnl': f"{pnl_pct:+.2f}%",
            'pos_ratio': f"{pos_ratio:.2f}%"
        })
        self.aggregate_nav.append({'dt': kline['dt'], 'nav': fund_value})
        current_dd = 0.0
        if self.aggregate_nav:
            nav_series = [x.get('nav', 0.0) for x in self.aggregate_nav]
            peak = max(nav_series) if nav_series else fund_value
            current_dd = ((peak - fund_value) / peak * 100) if peak > 0 else 0.0
        await self._emit('ministry_tick', {
            'cash': round(cash_total, 2),
            'available_pos_pct': max(0.0, 100.0 - pos_ratio),
            'main_strategy': active_strategy_id,
            'drawdown_pct': round(current_dd, 2),
            'compliance_status': compliance_status
        })

    async def run(self, start_date=None, end_date=None):
        if not start_date:
            start_date = datetime.now() - timedelta(days=365)
        if not end_date:
            end_date = datetime.now()

        await self._emit('system', {'msg': f"开始回测 {self.stock_code} ({start_date.date()} - {end_date.date()})..."})
        await self._emit('backtest_flow', {'module': '太子院', 'level': 'system', 'msg': f'校验标的与回测区间: {self.stock_code} {start_date.date()}~{end_date.date()}'})
        await self._emit('backtest_flow', {'module': '工部', 'level': 'system', 'msg': f'装载行情数据: {self.stock_code} {start_date.date()}~{end_date.date()}...'})
        
        # 1. Fetch Data
        provider_source = self.config.get("data_provider.source", "default")
        enable_fallback = bool(self.config.get("data_provider.enable_fallback", False))
        # Primary provider
        if provider_source == 'tushare':
            provider = TushareProvider(token=self.config.get("data_provider.tushare_token"))
        elif provider_source == 'akshare':
            provider = AkshareProvider()
        else:
            provider = DataProvider()
        df = provider.fetch_minute_data(self.stock_code, start_date, end_date)
        # Fallback chain if empty
        if df.empty and enable_fallback:
            # Try Tushare if token exists and wasn't primary
            token = self.config.get("data_provider.tushare_token")
            if token and provider_source != 'tushare':
                await self._emit('system', {'msg': f"主数据源无数据，尝试 Tushare 获取 {self.stock_code} 历史K线..."})
                try:
                    df = TushareProvider(token=token).fetch_minute_data(self.stock_code, start_date, end_date)
                except Exception:
                    df = pd.DataFrame()
        if df.empty and enable_fallback and provider_source != 'akshare':
            await self._emit('system', {'msg': f"Tushare 无数据，尝试 Akshare 获取 {self.stock_code} 历史K线..."})
            try:
                df = AkshareProvider().fetch_minute_data(self.stock_code, start_date, end_date)
            except Exception:
                df = pd.DataFrame()
        if df.empty:
            provider_msg = ""
            if provider_source == "default" and hasattr(provider, "last_error") and provider.last_error:
                provider_msg = f" 诊断: {provider.last_error}"
            fail_msg = f"无法获取 {self.stock_code} 的历史数据，回测终止。{provider_msg}".strip()
            await self._emit('system', {'msg': f"❌ {fail_msg}"})
            await self._emit('backtest_failed', {
                'msg': fail_msg,
                'stock': self.stock_code,
                'period': f"{start_date.date()} - {end_date.date()}",
                'provider_source': provider_source
            })
            return

        df = self.works.clean_data(df)
        total_bars = len(df)
        await self._emit('system', {'msg': f"已获取 {total_bars} 条K线数据，正在初始化策略..."})
        await self._emit('backtest_flow', {'module': '工部', 'level': 'system', 'msg': f'数据清洗完成，共 {total_bars} 条分钟K线'})
        day_end_dt_set = set(pd.to_datetime(df.groupby(df["dt"].dt.date)["dt"].max()).tolist())
        final_bar_dt = pd.to_datetime(df.iloc[-1]["dt"])
        for strategy in self.strategies:
            strategy.set_backtest_context(final_bar_dt=final_bar_dt)

        strategy_trigger_tf = {s.id: getattr(s, "trigger_timeframe", "1min") for s in self.strategies}
        needed_timeframes = sorted(set([tf for tf in strategy_trigger_tf.values() if tf != "1min"]))
        tf_dt_sets = {}
        for tf in needed_timeframes:
            tf_df = pd.DataFrame()
            try:
                if hasattr(provider, "fetch_kline_data"):
                    tf_df = provider.fetch_kline_data(self.stock_code, start_date, end_date, interval=tf)
            except Exception:
                tf_df = pd.DataFrame()
            if tf_df.empty:
                try:
                    tf_df = Indicators.resample(df, tf)
                except Exception:
                    tf_df = pd.DataFrame()
            if tf_df.empty or "dt" not in tf_df.columns:
                continue
            tf_df["dt"] = pd.to_datetime(tf_df["dt"])
            if tf != "D":
                tf_dt_sets[tf] = set(tf_df["dt"].tolist())
        if needed_timeframes:
            await self._emit('system', {'msg': f"策略周期映射已启用: {strategy_trigger_tf}"})
            await self._emit('backtest_flow', {'module': '中书省', 'level': 'system', 'msg': f'策略周期映射: {strategy_trigger_tf}'})

        # 2. Warm up strategies with initial data (optional, or just run)
        # Here we just run bar by bar
        
        # 3. Main Loop
        report_interval = max(1, total_bars // 50) # Report 50 times total
        
        for i, row in df.iterrows():
            # Check for cancellation (how? maybe checking a flag if running in task)
            # For now, just yield control
            if i % 100 == 0:
                await asyncio.sleep(0) # Yield
            kline = row
            
            if i % report_interval == 0:
                progress = int((i / total_bars) * 100)
                await self._emit('backtest_progress', {'progress': progress, 'current_date': str(row['dt'])})
                await self._emit('market', {'price': float(kline['close']), 'ma5': float(kline['close']) * 0.99, 'macd': 0.0, 'rsi': 50.0, 'time': str(row['dt'])})
                await self._emit_account_snapshot(kline, active_strategy_id=None, compliance_status="PASS")
            
            current_dt = pd.to_datetime(kline["dt"])
            runnable_strategy_ids = []
            for sid, tf in strategy_trigger_tf.items():
                if tf == "1min":
                    runnable_strategy_ids.append(sid)
                elif tf == "D":
                    if current_dt in day_end_dt_set:
                        runnable_strategy_ids.append(sid)
                else:
                    if current_dt in tf_dt_sets.get(tf, set()):
                        runnable_strategy_ids.append(sid)

            # Generate Signals
            strategy_context = {
                sid: {
                    "current_cash": float(self.strategy_revenues[sid].cash),
                    "last_price": float(kline.get("close", 0.0))
                }
                for sid in runnable_strategy_ids
                if sid in self.strategy_revenues
            }
            signals = self.secretariat.generate_signals(
                kline,
                runnable_strategy_ids=runnable_strategy_ids,
                strategy_context={"__by_strategy__": strategy_context}
            )
            if signals:
                await self._emit('backtest_flow', {
                    'module': '中书省',
                    'level': 'warning',
                    'msg': f"{current_dt} 触发 {len(signals)} 条候选交易信号（策略: {','.join([s['strategy_id'] for s in signals])}）"
                })
            
            for signal in signals:
                sid = signal['strategy_id']
                await self._emit('zhongshu', {
                    'msg': f"策略 {sid} 生成信号",
                    'details': f"> 标的: {signal['code']}<br>> 方向: {signal['direction']}<br>> 价格: {float(signal['price']):.2f}",
                    'status': 'bg-trading-blue'
                })
                
                # Check Risk
                # Approx fund value for backtest speed
                account = self.strategy_revenues.get(sid)
                if account is None:
                    continue
                current_fund_value = float(account.cash) + self.state_affairs.update_strategy_holdings_value(sid, {kline['code']: kline['close']})
                current_positions = self.state_affairs.positions.get(sid, {})
                
                approved, reason = self.chancellery.check_signal(signal, current_fund_value, current_positions, 0.0)
                await self._emit('menxia', {
                    'msg': "风控审核通过" if approved else "风控审核拒绝",
                    'details': f"> 策略: {sid}<br>> 结果: {'通过' if approved else '拒绝'}<br>> 原因: {reason}",
                    'status': 'bg-trading-green' if approved else 'bg-trading-red',
                    'decision': 'approved' if approved else 'rejected',
                    'strategy_id': sid,
                    'reason': reason
                })
                await self._emit('backtest_flow', {
                    'module': '门下省',
                    'level': 'success' if approved else 'danger',
                    'msg': f"策略 {sid} 风控{'通过' if approved else '拒绝'}：{reason}"
                })
                
                if approved:
                    await self._emit('shangshu', {
                        'msg': "准备执行交易指令",
                        'details': f"> 策略: {sid}<br>> 动作: {signal['direction']}<br>> 数量: {signal['qty']}",
                        'status': 'bg-trading-yellow'
                    })
                    executed = self.state_affairs.execute_order(sid, signal, kline, hu_bu_account=account)
                    if executed:
                        new_qty = self.state_affairs.positions[sid][signal['code']]['qty'] if signal['code'] in self.state_affairs.positions.get(sid, {}) else 0
                        self.secretariat.update_strategy_state(sid, signal['code'], new_qty)
                        await self._emit('backtest_flow', {
                            'module': '尚书省',
                            'level': 'success',
                            'msg': f"执行成交: 策略 {sid} {signal['direction']} {signal['code']} @ {float(signal['price']):.2f} x {signal['qty']}"
                        })
                        
                        # Emit trade log occasionally or accumulate?
                        # Sending every trade might flood WS if high freq.
                        # Let's send major trades.
                        await self._emit('backtest_trade', {
                            'dt': str(kline['dt']),
                            'strategy': sid,
                            'code': signal['code'],
                            'dir': signal['direction'],
                            'price': signal['price'],
                            'qty': signal['qty']
                        })
                        await self._emit_account_snapshot(kline, active_strategy_id=sid, compliance_status="PASS")

            # Check Stops
            triggered_orders = self.state_affairs.check_stops(kline)
            for order in triggered_orders:
                account = self.strategy_revenues.get(order['strategy_id'])
                if account is None:
                    continue
                executed = self.state_affairs.execute_order(order['strategy_id'], order, kline, hu_bu_account=account)
                if not executed:
                    continue
                self.secretariat.update_strategy_state(order['strategy_id'], order['code'], 0)
                await self._emit('backtest_flow', {
                            'module': '兵部',
                            'level': 'warning',
                            'msg': f"触发止损/止盈: 策略 {order['strategy_id']} {order['code']} {order['direction']} x {order['qty']}"
                        })
                await self._emit('backtest_trade', {
                            'dt': str(kline['dt']),
                            'strategy': order['strategy_id'],
                            'code': order['code'],
                            'dir': order['direction'],
                            'price': order['price'],
                            'qty': order['qty'],
                            'reason': 'STOP'
                        })
                await self._emit_account_snapshot(kline, active_strategy_id=order['strategy_id'], compliance_status="PASS")

        # 4. Generate Report
        await self._emit('backtest_progress', {'progress': 100, 'current_date': 'Done'})
        
        reports = []
        for s in self.strategies:
            account = self.strategy_revenues.get(s.id)
            if account is None:
                continue
            report = self.rites.generate_report(
                s.id,
                account,
                self.justice,
                self.strategy_initial_capital,
                start_date=start_date,
                end_date=end_date
            )
            reports.append(report)
            strategy_transactions = list(account.transactions)
            formatted = self.rites.generate_backtest_report(
                strategy_id=s.id,
                transactions=strategy_transactions,
                initial_capital=self.strategy_initial_capital,
                start_date=start_date,
                end_date=end_date
            )
            await self._emit('backtest_strategy_report', formatted)
            
        ranking = self.rites.generate_ranking(reports)
        
        # Convert ranking to dict for JSON
        ranking_dict = ranking.to_dict('records')
        
        await self._emit('backtest_result', {
            'stock': self.stock_code,
            'period': f"{start_date.date()} - {end_date.date()}",
            'ranking': ranking_dict,
            'total_trades': sum(r['total_trades'] for r in reports)
        })
        await self._emit('backtest_flow', {'module': '礼部', 'level': 'success', 'msg': f'回测结算完成，总交易 {sum(r["total_trades"] for r in reports)} 笔'})
        
        await self._emit('system', {'msg': f"回测完成。"})
