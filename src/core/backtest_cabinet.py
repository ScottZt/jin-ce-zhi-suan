
import asyncio
import pandas as pd
from datetime import datetime, timedelta
from time import perf_counter
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
    _tf_cache = {}
    _tf_cache_limit = 24

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
        self._event_queue = None
        self._event_task = None
        
        for s in self.strategies:
            self.personnel.register_strategy(s)

    def _cache_key(self, start_date, end_date, interval, provider_source):
        return (
            str(self.stock_code),
            str(provider_source),
            str(interval),
            start_date.strftime("%Y-%m-%d"),
            end_date.strftime("%Y-%m-%d")
        )

    def _cache_get(self, start_date, end_date, interval, provider_source):
        key = self._cache_key(start_date, end_date, interval, provider_source)
        df = BacktestCabinet._tf_cache.get(key)
        if isinstance(df, pd.DataFrame) and not df.empty:
            return df.copy()
        return pd.DataFrame()

    def _cache_set(self, start_date, end_date, interval, provider_source, df):
        if not isinstance(df, pd.DataFrame) or df.empty:
            return
        key = self._cache_key(start_date, end_date, interval, provider_source)
        BacktestCabinet._tf_cache[key] = df.copy()
        if len(BacktestCabinet._tf_cache) > BacktestCabinet._tf_cache_limit:
            first_key = next(iter(BacktestCabinet._tf_cache))
            if first_key != key:
                BacktestCabinet._tf_cache.pop(first_key, None)

    def _normalize_trigger_tf(self, tf):
        x = str(tf or "1min").strip()
        if not x:
            return "1min"
        low = x.lower()
        if low in ("d", "1d", "day", "daily"):
            return "D"
        return x

    async def _force_close_positions_at_end(self, kline):
        if not bool(self.config.get("execution.force_close_on_backtest_end", True)):
            return
        closed_any = False
        for sid, stocks in list(self.state_affairs.positions.items()):
            account = self.strategy_revenues.get(sid)
            if account is None:
                continue
            for code, pos in list(stocks.items()):
                qty = int(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                order = {
                    "strategy_id": sid,
                    "code": code,
                    "dt": kline["dt"],
                    "direction": "SELL",
                    "qty": qty,
                    "price": float(kline.get("close", 0) or 0),
                    "reason": "FORCE_CLOSE_END"
                }
                executed = self.state_affairs.execute_order(sid, order, kline, hu_bu_account=account)
                if not executed:
                    continue
                self.secretariat.update_strategy_state(sid, code, 0)
                closed_any = True
                await self._emit('backtest_flow', {
                    'module': '尚书省',
                    'level': 'warning',
                    'msg': f"回测结束强制平仓: 策略 {sid} SELL {code} x {qty}"
                })
                await self._emit('backtest_trade', {
                    'dt': str(kline['dt']),
                    'strategy': sid,
                    'code': code,
                    'dir': 'SELL',
                    'price': float(kline.get('close', 0) or 0),
                    'qty': qty,
                    'reason': 'FORCE_CLOSE_END'
                })
        if closed_any:
            await self._emit_account_snapshot(kline, active_strategy_id=None, compliance_status="PASS")

    async def _emit_loop(self):
        while True:
            item = await self._event_queue.get()
            if item is None:
                break
            event_type, data = item
            if self.event_callback:
                await self.event_callback(event_type, data)

    async def _start_event_pump(self):
        if not self.event_callback:
            return
        self._event_queue = asyncio.Queue(maxsize=20000)
        self._event_task = asyncio.create_task(self._emit_loop())

    async def _stop_event_pump(self):
        if self._event_queue is None or self._event_task is None:
            return
        await self._event_queue.put(None)
        await self._event_task
        self._event_queue = None
        self._event_task = None

    async def _emit(self, event_type, data):
        if not self.event_callback:
            return
        if self._event_queue is None:
            await self.event_callback(event_type, data)
            return
        try:
            self._event_queue.put_nowait((event_type, data))
        except asyncio.QueueFull:
            await self._event_queue.put((event_type, data))

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
        total_started_at = perf_counter()
        perf_data_fetch_ms = 0
        perf_period_build_ms = 0
        perf_main_loop_ms = 0
        perf_settlement_ms = 0
        await self._start_event_pump()
        try:
            await self._emit('system', {'msg': f"开始回测 {self.stock_code} ({start_date.date()} - {end_date.date()})..."})
            await self._emit('backtest_flow', {'module': '太子院', 'level': 'system', 'msg': f'校验标的与回测区间: {self.stock_code} {start_date.date()}~{end_date.date()}'})
            await self._emit('backtest_flow', {'module': '工部', 'level': 'system', 'msg': f'装载行情数据: {self.stock_code} {start_date.date()}~{end_date.date()}...'})
            stage_started_at = perf_counter()
            provider_source = self.config.get("data_provider.source", "default")
            enable_fallback = bool(self.config.get("data_provider.enable_fallback", False))
            if provider_source == 'tushare':
                provider = TushareProvider(token=self.config.get("data_provider.tushare_token"))
            elif provider_source == 'akshare':
                provider = AkshareProvider()
            else:
                provider = DataProvider()
            df = self._cache_get(start_date, end_date, "1min", provider_source)
            if df.empty:
                df = provider.fetch_minute_data(self.stock_code, start_date, end_date)
                if df.empty and enable_fallback:
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
                self._cache_set(start_date, end_date, "1min", provider_source, df)
            total_bars = len(df)
            perf_data_fetch_ms = int((perf_counter() - stage_started_at) * 1000)
            await self._emit('system', {'msg': f"已获取 {total_bars} 条K线数据，正在初始化策略..."})
            await self._emit('backtest_flow', {'module': '工部', 'level': 'system', 'msg': f'数据清洗完成，共 {total_bars} 条分钟K线'})
            day_end_dt_set = set(pd.to_datetime(df.groupby(df["dt"].dt.date)["dt"].max()).tolist())
            final_bar_dt = pd.to_datetime(df.iloc[-1]["dt"])
            for strategy in self.strategies:
                strategy.set_backtest_context(final_bar_dt=final_bar_dt)
            stage_started_at = perf_counter()
            strategy_trigger_tf = {s.id: self._normalize_trigger_tf(getattr(s, "trigger_timeframe", "1min")) for s in self.strategies}
            needed_timeframes = sorted(set([tf for tf in strategy_trigger_tf.values() if tf != "1min"]))
            tf_dt_sets = {}
            tf_row_maps = {}
            tf_date_row_maps = {}
            for tf in needed_timeframes:
                tf_df = self._cache_get(start_date, end_date, tf, provider_source)
                if tf_df.empty:
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
                    if not tf_df.empty:
                        self._cache_set(start_date, end_date, tf, provider_source, tf_df)
                if tf_df.empty or "dt" not in tf_df.columns:
                    continue
                tf_df["dt"] = pd.to_datetime(tf_df["dt"])
                tf_df = tf_df.sort_values("dt")
                row_map = {}
                date_map = {}
                for _, rr in tf_df.iterrows():
                    dtt = pd.to_datetime(rr["dt"])
                    row_obj = rr.to_dict()
                    row_map[dtt] = row_obj
                    date_map[dtt.date()] = row_obj
                tf_row_maps[tf] = row_map
                tf_date_row_maps[tf] = date_map
                if tf != "D":
                    tf_dt_sets[tf] = set(tf_df["dt"].tolist())
            perf_period_build_ms = int((perf_counter() - stage_started_at) * 1000)
            if needed_timeframes:
                await self._emit('system', {'msg': f"策略周期映射已启用: {strategy_trigger_tf}"})
                await self._emit('backtest_flow', {'module': '中书省', 'level': 'system', 'msg': f'策略周期映射: {strategy_trigger_tf}'})
            report_interval = max(1, total_bars // 50)
            op_counter = 0
            close_series = pd.to_numeric(df["close"], errors="coerce").ffill().bfill()
            ma5_series = Indicators.MA(close_series, 5).fillna(close_series)
            _, _, macd_series = Indicators.MACD(close_series)
            macd_series = macd_series.fillna(0.0)
            rsi_series = Indicators.RSI(close_series).fillna(50.0)
            col_idx = {c: i for i, c in enumerate(df.columns)}
            stage_started_at = perf_counter()
            for i, row in enumerate(df.itertuples(index=False, name=None)):
                if i % 10 == 0:
                    await asyncio.sleep(0)
                kline = {
                    "code": row[col_idx.get("code")],
                    "dt": row[col_idx.get("dt")],
                    "open": row[col_idx.get("open")],
                    "high": row[col_idx.get("high")],
                    "low": row[col_idx.get("low")],
                    "close": row[col_idx.get("close")],
                    "vol": row[col_idx.get("vol")] if "vol" in col_idx else row[col_idx.get("volume")],
                    "amount": row[col_idx.get("amount")] if "amount" in col_idx else row[col_idx.get("turnover")]
                }
                if "volume" not in kline and "vol" in kline:
                    kline["volume"] = kline["vol"]
                if "vol" not in kline and "volume" in kline:
                    kline["vol"] = kline["volume"]
                if "turnover" not in kline and "amount" in kline:
                    kline["turnover"] = kline["amount"]
                if "amount" not in kline and "turnover" in kline:
                    kline["amount"] = kline["turnover"]
                current_dt = pd.to_datetime(kline["dt"])
                runnable_strategy_ids = []
                strategy_kline_map = {}
                for sid, tf in strategy_trigger_tf.items():
                    if tf == "1min":
                        runnable_strategy_ids.append(sid)
                        strategy_kline_map[sid] = kline
                    elif tf == "D":
                        if current_dt in day_end_dt_set:
                            runnable_strategy_ids.append(sid)
                            r = tf_date_row_maps.get("D", {}).get(current_dt.date())
                            if isinstance(r, dict):
                                k = {
                                    "code": self.stock_code,
                                    "dt": pd.to_datetime(r.get("dt", current_dt)),
                                    "open": float(r.get("open", r.get("close", 0)) or 0),
                                    "high": float(r.get("high", r.get("close", 0)) or 0),
                                    "low": float(r.get("low", r.get("close", 0)) or 0),
                                    "close": float(r.get("close", 0) or 0),
                                    "vol": float(r.get("vol", r.get("volume", 0)) or 0),
                                    "amount": float(r.get("amount", r.get("turnover", 0)) or 0)
                                }
                                k["volume"] = k["vol"]
                                k["turnover"] = k["amount"]
                                strategy_kline_map[sid] = k
                            else:
                                strategy_kline_map[sid] = kline
                    else:
                        if current_dt in tf_dt_sets.get(tf, set()):
                            runnable_strategy_ids.append(sid)
                            r = tf_row_maps.get(tf, {}).get(current_dt)
                            if isinstance(r, dict):
                                k = {
                                    "code": self.stock_code,
                                    "dt": pd.to_datetime(r.get("dt", current_dt)),
                                    "open": float(r.get("open", r.get("close", 0)) or 0),
                                    "high": float(r.get("high", r.get("close", 0)) or 0),
                                    "low": float(r.get("low", r.get("close", 0)) or 0),
                                    "close": float(r.get("close", 0) or 0),
                                    "vol": float(r.get("vol", r.get("volume", 0)) or 0),
                                    "amount": float(r.get("amount", r.get("turnover", 0)) or 0)
                                }
                                k["volume"] = k["vol"]
                                k["turnover"] = k["amount"]
                                strategy_kline_map[sid] = k
                            else:
                                strategy_kline_map[sid] = kline
                if i % report_interval == 0:
                    progress = int((i / total_bars) * 100)
                    await self._emit('backtest_progress', {'progress': progress, 'current_date': str(kline['dt'])})
                    runnable_tf = sorted(set([strategy_trigger_tf.get(s, "1min") for s in runnable_strategy_ids]))
                    await self._emit('market', {
                        'price': float(kline['close']),
                        'ma5': float(ma5_series.iloc[i]),
                        'macd': float(macd_series.iloc[i]),
                        'rsi': float(rsi_series.iloc[i]),
                        'time': str(kline['dt']),
                        'kline_timeframe': '1分钟驱动',
                        'kline_dt': str(kline['dt']),
                        'strategy_timeframes': strategy_trigger_tf,
                        'runnable_strategy_ids': runnable_strategy_ids,
                        'runnable_timeframes': runnable_tf
                    })
                    await self._emit_account_snapshot(kline, active_strategy_id=None, compliance_status="PASS")
                strategy_context = {
                    sid: {
                        "current_cash": float(self.strategy_revenues[sid].cash),
                        "last_price": float((strategy_kline_map.get(sid, kline) or {}).get("close", 0.0))
                    }
                    for sid in runnable_strategy_ids
                    if sid in self.strategy_revenues
                }
                signals = self.secretariat.generate_signals(
                    kline,
                    runnable_strategy_ids=runnable_strategy_ids,
                    strategy_context={"__by_strategy__": strategy_context, "__kline_by_strategy__": strategy_kline_map}
                )
                if signals:
                    await self._emit('backtest_flow', {
                        'module': '中书省',
                        'level': 'warning',
                        'msg': f"{current_dt} 触发 {len(signals)} 条候选交易信号（策略: {','.join([s['strategy_id'] for s in signals])}）"
                    })
                for signal in signals:
                    op_counter += 1
                    if op_counter % 20 == 0:
                        await asyncio.sleep(0)
                    sid = signal['strategy_id']
                    await self._emit('zhongshu', {
                        'msg': f"策略 {sid} 生成信号",
                        'details': f"> 标的: {signal['code']}<br>> 方向: {signal['direction']}<br>> 价格: {float(signal['price']):.2f}",
                        'status': 'bg-trading-blue'
                    })
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
                            await self._emit('backtest_trade', {
                                'dt': str(kline['dt']),
                                'strategy': sid,
                                'code': signal['code'],
                                'dir': signal['direction'],
                                'price': signal['price'],
                                'qty': signal['qty']
                            })
                            await self._emit_account_snapshot(kline, active_strategy_id=sid, compliance_status="PASS")
                triggered_orders = self.state_affairs.check_stops(kline)
                for order in triggered_orders:
                    op_counter += 1
                    if op_counter % 20 == 0:
                        await asyncio.sleep(0)
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
            perf_main_loop_ms = int((perf_counter() - stage_started_at) * 1000)
            await self._emit('backtest_progress', {'progress': 100, 'current_date': 'Done'})
            stage_started_at = perf_counter()
            last_row = df.iloc[-1]
            final_kline = {
                "dt": pd.to_datetime(last_row["dt"]),
                "code": self.stock_code,
                "open": float(last_row.get("open", last_row.get("close", 0)) or 0),
                "high": float(last_row.get("high", last_row.get("close", 0)) or 0),
                "low": float(last_row.get("low", last_row.get("close", 0)) or 0),
                "close": float(last_row.get("close", 0) or 0),
                "volume": float(last_row.get("volume", last_row.get("vol", 0)) or 0)
            }
            await self._force_close_positions_at_end(final_kline)
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
                    end_date=end_date,
                    summary_metrics=report
                )
                tf = strategy_trigger_tf.get(s.id, "1min")
                formatted["kline_type"] = tf
                if tf == "D":
                    formatted["period_label"] = "日线"
                elif tf.endswith("min"):
                    formatted["period_label"] = tf.replace("min", "分钟")
                else:
                    formatted["period_label"] = tf
                await self._emit('backtest_strategy_report', formatted)
            ranking = self.rites.generate_ranking(reports)
            ranking_dict = ranking.to_dict('records')
            perf_settlement_ms = int((perf_counter() - stage_started_at) * 1000)
            perf_total_ms = int((perf_counter() - total_started_at) * 1000)
            await self._emit('backtest_result', {
                'stock': self.stock_code,
                'period': f"{start_date.date()} - {end_date.date()}",
                'ranking': ranking_dict,
                'total_trades': sum(r['total_trades'] for r in reports),
                'perf_ms': {
                    'data_fetch': perf_data_fetch_ms,
                    'period_build': perf_period_build_ms,
                    'main_loop': perf_main_loop_ms,
                    'settlement': perf_settlement_ms,
                    'total': perf_total_ms
                }
            })
            await self._emit('backtest_flow', {
                'module': '礼部',
                'level': 'system',
                'msg': f"耗时统计(ms): 数据获取={perf_data_fetch_ms} 周期构建={perf_period_build_ms} 主循环={perf_main_loop_ms} 结算={perf_settlement_ms} 总计={perf_total_ms}"
            })
            await self._emit('backtest_flow', {'module': '礼部', 'level': 'success', 'msg': f'回测结算完成，总交易 {sum(r["total_trades"] for r in reports)} 笔'})
            await self._emit('system', {'msg': f"回测完成。"})
        finally:
            await self._stop_event_pump()
