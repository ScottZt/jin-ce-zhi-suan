# src/core/live_cabinet.py
import time
import os
import json
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
from src.utils.mysql_provider import MysqlProvider
from src.utils.indicators import Indicators
from src.utils.config_loader import ConfigLoader

class LiveCabinet:
    def __init__(self, stock_code, initial_capital=1000000.0, provider_type='default', tushare_token=None, event_callback=None, strategy_ids=None):
        self.stock_code = stock_code
        self.event_callback = event_callback # Callback for UI updates
        self.config = ConfigLoader.reload()
        self.provider_type = str(provider_type or "default").lower()
        self.tushare_token = tushare_token or self.config.get("data_provider.tushare_token", "")
        
        if self.provider_type == 'tushare':
            self.provider = TushareProvider(token=self.tushare_token, event_callback=self._emit_event)
            print("🌐 Data Source: Tushare Pro")
        elif self.provider_type == 'akshare':
            self.provider = AkshareProvider()
            print("🌐 Data Source: Akshare (Free)")
        elif self.provider_type == 'mysql':
            self.provider = MysqlProvider()
            print("🌐 Data Source: MySQL")
        else:
            self.provider = DataProvider()
            print("🌐 Data Source: Default API")
        self._tushare_fallback_provider = None
        if self.provider_type != "tushare" and self.tushare_token:
            try:
                self._tushare_fallback_provider = TushareProvider(token=self.tushare_token, event_callback=self._emit_event)
            except Exception:
                self._tushare_fallback_provider = None
        sync_enabled = bool(self.config.get("live_monitoring.realtime_sync_to_default.enabled", True))
        sync_sources = self.config.get("live_monitoring.realtime_sync_to_default.sources", ["tushare", "akshare", "mysql", "postgresql"])
        sync_on_duplicate = str(self.config.get("live_monitoring.realtime_sync_to_default.on_duplicate", "update") or "update").strip().lower()
        if sync_on_duplicate not in {"ignore", "update", "replace"}:
            sync_on_duplicate = "update"
        if not isinstance(sync_sources, list):
            sync_sources = ["tushare", "akshare", "mysql", "postgresql"]
        source_allow = {str(x or "").strip().lower() for x in sync_sources if str(x or "").strip()}
        self._rt_sync_to_default_enabled = bool(sync_enabled and (self.provider_type in source_allow))
        self._rt_sync_to_default_on_duplicate = sync_on_duplicate
        self._rt_sync_to_default_provider = DataProvider() if self._rt_sync_to_default_enabled else None
        self._rt_synced_bar_keys = set()
        self._rt_sync_last_error = ""
        
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
        picked_ids = []
        if isinstance(strategy_ids, list):
            seen = set()
            for item in strategy_ids:
                sid = str(item or "").strip()
                if (not sid) or sid in seen:
                    continue
                seen.add(sid)
                picked_ids.append(sid)
        use_active_filter = not bool(picked_ids)
        all_strategies = strategy_factory_module.create_strategies(apply_active_filter=use_active_filter)
        if picked_ids:
            allowed = set(picked_ids)
            self.strategies = [s for s in all_strategies if str(s.id).strip() in allowed]
            if not self.strategies:
                self.strategies = all_strategies
        else:
            self.strategies = all_strategies
        self.all_strategies = self.strategies
        self.secretariat = ZhongshuSheng(self.strategies)
        self.active_strategy_ids = [s.id for s in self.strategies] # Default all active
        self.strategy_trigger_tf = {s.id: self._normalize_trigger_tf(getattr(s, "trigger_timeframe", "1min")) for s in self.strategies}
        
        for s in self.strategies:
            self.personnel.register_strategy(s)
            
        self.last_dt = None
        self.daily_data_buffer = []
        self.today_archived = False
        self.peak_fund_value = float(initial_capital)
        self._live_alert_last = {}
        self.prepared_kline_latest = {}
        self._direct_tf_support = {}
        self.startup_kline_freshness = {}
        self._last_kline_fetch_notice_ts = 0.0
        warmup_retry_raw = int(self.config.get("system.live_warmup_retry_sec", 30) or 30)
        self._warmup_retry_sec = max(10, min(warmup_retry_raw, 600))
        self._summary_day = ""
        self._day_start_fund_value = None
        self._intraday_fund_curve = []
        self._intraday_signal_counter = {}
        self._daily_summary_sent_day = ""
        self._last_summary_tick_dt = None
        self._daily_auto_stop_day = ""
        self._fund_pool_state_file = os.path.join("data", "live_fund_pool", f"{str(self.stock_code or '').upper()}.json")
        self._restore_virtual_fund_pool()

    def _sum_holdings_value(self):
        total = 0.0
        for _, stocks in self.state_affairs.positions.items():
            if not isinstance(stocks, dict):
                continue
            for _, pos in stocks.items():
                if not isinstance(pos, dict):
                    continue
                qty = int(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                mv = float(pos.get("market_value", 0.0) or 0.0)
                if mv <= 0:
                    mv = float(pos.get("avg_price", 0.0) or 0.0) * qty
                total += mv
        return float(total)

    def _position_snapshot_rows(self):
        rows = []
        for strategy_id, stocks in self.state_affairs.positions.items():
            if not isinstance(stocks, dict):
                continue
            for code, pos in stocks.items():
                if not isinstance(pos, dict):
                    continue
                qty = int(pos.get("qty", 0) or 0)
                if qty <= 0:
                    continue
                avg_price = float(pos.get("avg_price", 0.0) or 0.0)
                market_value = float(pos.get("market_value", 0.0) or 0.0)
                if market_value <= 0:
                    market_value = avg_price * qty
                rows.append({
                    "strategy_id": str(strategy_id),
                    "code": str(code),
                    "qty": qty,
                    "avg_price": avg_price,
                    "market_value": market_value,
                    "stop_loss": pos.get("stop_loss"),
                    "take_profit": pos.get("take_profit")
                })
        rows.sort(key=lambda x: (x.get("code", ""), x.get("strategy_id", "")))
        return rows

    def get_fund_pool_snapshot(self, include_transactions=False, tx_limit=200):
        holdings_value = self._sum_holdings_value()
        cash = float(self.revenue.cash or 0.0)
        fund_value = cash + holdings_value
        tx_all = list(self.revenue.transactions or [])
        tx_count = len(tx_all)
        tx_view = tx_all[-max(1, int(tx_limit or 1)):] if include_transactions else tx_all[-20:]
        tx_list = []
        for tx in tx_view:
            if not isinstance(tx, dict):
                continue
            tx_list.append({
                "strategy_id": str(tx.get("strategy_id", "")),
                "dt": str(tx.get("dt", "")),
                "direction": str(tx.get("direction", "")),
                "price": float(tx.get("price", 0.0) or 0.0),
                "quantity": int(tx.get("quantity", 0) or 0),
                "amount": float(tx.get("amount", 0.0) or 0.0),
                "cost": float(tx.get("cost", 0.0) or 0.0),
                "commission": float(tx.get("commission", 0.0) or 0.0),
                "stamp_duty": float(tx.get("stamp_duty", 0.0) or 0.0),
                "transfer_fee": float(tx.get("transfer_fee", 0.0) or 0.0),
                "pnl": float(tx.get("pnl", 0.0) or 0.0)
            })
        realized_pnl = float(sum(float(x.get("pnl", 0.0) or 0.0) for x in tx_all if str(x.get("direction", "")).upper() == "SELL"))
        total_cost = float(sum(float(x.get("cost", 0.0) or 0.0) for x in tx_all))
        today_rt_cache_bars = 0
        today_rt_cache_last_dt = ""
        provider_last_error = str(getattr(self.provider, "last_error", "") or "")
        stats_fn = getattr(self.provider, "get_today_rt_cache_stats", None)
        if callable(stats_fn):
            try:
                rt_stats = stats_fn(self.stock_code)
                if isinstance(rt_stats, dict):
                    today_rt_cache_bars = int(rt_stats.get("bars", 0) or 0)
                    today_rt_cache_last_dt = str(rt_stats.get("last_dt", "") or "")
            except Exception:
                pass
        return {
            "stock_code": str(self.stock_code or "").upper(),
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "initial_capital": float(self.revenue.initial_capital or 0.0),
            "cash": round(cash, 4),
            "holdings_value": round(holdings_value, 4),
            "fund_value": round(fund_value, 4),
            "position_count": len(self._position_snapshot_rows()),
            "positions": self._position_snapshot_rows(),
            "trade_count": tx_count,
            "trade_details": tx_list,
            "realized_pnl": round(realized_pnl, 4),
            "fee_summary": {
                "total_cost": round(total_cost, 4),
                "total_commission": round(float(self.revenue.total_commission or 0.0), 4),
                "total_stamp_duty": round(float(self.revenue.total_stamp_duty or 0.0), 4),
                "total_transfer_fee": round(float(self.revenue.total_transfer_fee or 0.0), 4)
            },
            "peak_fund_value": round(float(self.peak_fund_value or 0.0), 4),
            "today_rt_cache_bars": today_rt_cache_bars,
            "today_rt_cache_last_dt": today_rt_cache_last_dt,
            "provider_last_error": provider_last_error
        }

    async def _emit_account_snapshot(self, holdings_value=None):
        if holdings_value is None:
            holdings_value = self._sum_holdings_value()
        holdings = float(holdings_value or 0.0)
        assets = float(self.revenue.cash or 0.0) + holdings
        pos_ratio = (holdings / assets * 100.0) if assets > 0 else 0.0
        day_start_assets = float(self._day_start_fund_value or 0.0)
        if day_start_assets <= 0:
            day_start_assets = float(assets)
        pnl_amount = float(assets) - float(day_start_assets)
        pnl_pct = (pnl_amount / float(day_start_assets) * 100.0) if day_start_assets > 0 else 0.0
        await self._emit_event('account', {
            'assets': round(assets, 2),
            'cash': round(float(self.revenue.cash or 0.0), 2),
            'day_start_assets': round(day_start_assets, 2),
            'pnl_amount': round(pnl_amount, 2),
            'pnl_pct': round(pnl_pct, 4),
            'pnl': f"{pnl_pct:+.2f}%",
            'pos_ratio': f"{pos_ratio:.2f}%"
        })

    def _persist_virtual_fund_pool(self):
        os.makedirs(os.path.dirname(self._fund_pool_state_file), exist_ok=True)
        snapshot = self.get_fund_pool_snapshot(include_transactions=True, tx_limit=max(2000, len(self.revenue.transactions or [])))
        payload = {
            "version": 1,
            "state": snapshot,
            "positions_state": self.state_affairs.positions,
            "transactions_all": list(self.revenue.transactions or [])
        }
        with open(self._fund_pool_state_file, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)

    def _restore_virtual_fund_pool(self):
        if not os.path.exists(self._fund_pool_state_file):
            self._persist_virtual_fund_pool()
            return False
        try:
            with open(self._fund_pool_state_file, "r", encoding="utf-8") as f:
                payload = json.load(f)
            state = payload.get("state", {}) if isinstance(payload, dict) else {}
            tx_all = payload.get("transactions_all", []) if isinstance(payload, dict) else []
            positions_state = payload.get("positions_state", {}) if isinstance(payload, dict) else {}
            if isinstance(state, dict):
                self.revenue.initial_capital = float(state.get("initial_capital", self.revenue.initial_capital) or self.revenue.initial_capital)
                self.revenue.cash = float(state.get("cash", self.revenue.cash) or self.revenue.cash)
                fee = state.get("fee_summary", {})
                if isinstance(fee, dict):
                    self.revenue.total_commission = float(fee.get("total_commission", 0.0) or 0.0)
                    self.revenue.total_stamp_duty = float(fee.get("total_stamp_duty", 0.0) or 0.0)
                    self.revenue.total_transfer_fee = float(fee.get("total_transfer_fee", 0.0) or 0.0)
                self.peak_fund_value = max(float(self.peak_fund_value or 0.0), float(state.get("peak_fund_value", self.peak_fund_value) or 0.0))
            if isinstance(tx_all, list):
                self.revenue.transactions = tx_all
            if isinstance(positions_state, dict):
                self.state_affairs.positions = positions_state
            return True
        except Exception:
            return False

    def _normalize_trigger_tf(self, tf):
        t = str(tf or "1min").strip()
        if t in {"1m", "1min", "1分钟"}:
            return "1min"
        if t in {"5m", "5min"}:
            return "5min"
        if t in {"10m", "10min"}:
            return "10min"
        if t in {"15m", "15min"}:
            return "15min"
        if t in {"30m", "30min"}:
            return "30min"
        if t in {"60m", "60min", "1h"}:
            return "60min"
        if t.upper() in {"D", "1D", "DAY"}:
            return "D"
        return "1min"

    def _required_timeframes(self):
        active_set = {str(x) for x in (self.active_strategy_ids or [])}
        tfs = {"1min"}
        for s in self.strategies:
            if active_set and str(s.id) not in active_set:
                continue
            tfs.add(self._normalize_trigger_tf(getattr(s, "trigger_timeframe", "1min")))
        return sorted(tfs, key=lambda x: ["1min", "5min", "10min", "15min", "30min", "60min", "D"].index(x) if x in {"1min", "5min", "10min", "15min", "30min", "60min", "D"} else 99)

    def _tf_resample_rule(self, timeframe):
        tf = self._normalize_trigger_tf(timeframe)
        mapping = {
            "1min": "1min",
            "5min": "5min",
            "10min": "10min",
            "15min": "15min",
            "30min": "30min",
            "60min": "60min",
            "D": "1D"
        }
        return mapping.get(tf, "1min")

    def _tf_span_days(self, timeframe):
        tf = self._normalize_trigger_tf(timeframe)
        mapping = {
            "1min": 3,
            "5min": 8,
            "10min": 12,
            "15min": 14,
            "30min": 20,
            "60min": 35,
            "D": 450
        }
        return int(mapping.get(tf, 8))

    def _minute_df_from_buffer(self, current_dt=None):
        if not isinstance(self.daily_data_buffer, list) or not self.daily_data_buffer:
            return pd.DataFrame()
        df = pd.DataFrame(self.daily_data_buffer)
        if df.empty or "dt" not in df.columns:
            return pd.DataFrame()
        df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
        df = df.dropna(subset=["dt"]).sort_values("dt")
        if current_dt is not None:
            dt_cut = self._to_naive_ts(current_dt)
            df = df[df["dt"] <= dt_cut]
        if df.empty:
            return pd.DataFrame()
        if "open" not in df.columns:
            df["open"] = pd.to_numeric(df.get("close"), errors="coerce")
        if "high" not in df.columns:
            df["high"] = pd.to_numeric(df.get("close"), errors="coerce")
        if "low" not in df.columns:
            df["low"] = pd.to_numeric(df.get("close"), errors="coerce")
        if "close" not in df.columns:
            return pd.DataFrame()
        if "vol" not in df.columns:
            df["vol"] = pd.to_numeric(df.get("volume", 0.0), errors="coerce").fillna(0.0)
        if "amount" not in df.columns:
            df["amount"] = pd.to_numeric(df.get("turnover", 0.0), errors="coerce").fillna(0.0)
        for col in ["open", "high", "low", "close", "vol", "amount"]:
            df[col] = pd.to_numeric(df[col], errors="coerce")
        df = df.dropna(subset=["close"])
        return df[["dt", "open", "high", "low", "close", "vol", "amount"]]

    def _resample_bar_from_1min(self, timeframe, current_dt):
        tf = self._normalize_trigger_tf(timeframe)
        if tf == "1min":
            return None
        df = self._minute_df_from_buffer(current_dt=current_dt)
        if df.empty:
            return None
        df = df.set_index("dt").sort_index()
        rule = self._tf_resample_rule(tf)
        agg = df.resample(rule, label="right", closed="right").agg({
            "open": "first",
            "high": "max",
            "low": "min",
            "close": "last",
            "vol": "sum",
            "amount": "sum"
        }).dropna(subset=["close"])
        if agg.empty:
            return None
        dt_cut = self._to_naive_ts(current_dt)
        agg = agg[agg.index <= dt_cut]
        if agg.empty:
            return None
        last_dt = agg.index[-1]
        row = agg.iloc[-1]
        bar = {
            "code": str(self.stock_code),
            "dt": self._to_naive_ts(last_dt),
            "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
            "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
            "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
            "close": float(row.get("close", 0.0) or 0.0),
            "vol": float(row.get("vol", 0.0) or 0.0),
            "amount": float(row.get("amount", 0.0) or 0.0)
        }
        bar["volume"] = bar["vol"]
        bar["turnover"] = bar["amount"]
        return bar

    def _fetch_latest_bar_for_timeframe(self, timeframe, current_dt):
        tf = self._normalize_trigger_tf(timeframe)
        if tf == "1min":
            return None
        if self._direct_tf_support.get(tf) is False:
            return self._resample_bar_from_1min(tf, current_dt)
        end_time = self._to_naive_ts(current_dt)
        if pd.isna(end_time):
            end_time = pd.Timestamp(datetime.now())
        start_time = end_time - timedelta(days=self._tf_span_days(tf))
        providers = [self.provider]
        if self._tushare_fallback_provider is not None:
            providers.append(self._tushare_fallback_provider)
        for provider in providers:
            df = self._fetch_kline_with_provider(provider, tf, start_time, end_time)
            if df is None or df.empty:
                continue
            try:
                cleaned = self.works.clean_data(df)
                if cleaned is None or cleaned.empty:
                    continue
                cleaned["dt"] = pd.to_datetime(cleaned["dt"], errors="coerce")
                cleaned = cleaned.dropna(subset=["dt"]).sort_values("dt")
                cleaned = cleaned[cleaned["dt"] <= end_time]
                if cleaned.empty:
                    continue
                bar = self._build_bar_from_df_row(cleaned.iloc[-1].to_dict())
                if bar is not None:
                    self._direct_tf_support[tf] = True
                    return bar
            except Exception:
                continue
        self._direct_tf_support[tf] = False
        return self._resample_bar_from_1min(tf, end_time)

    def _build_strategy_context(self, current_dt, base_bar, runnable_strategy_ids):
        runnable = list(runnable_strategy_ids or [])
        if not runnable:
            return {
                "current_cash": float(self.revenue.cash),
                "last_price": float(base_bar.get("close", 0.0))
            }
        tf_cache = {"1min": base_bar}
        sid_tf = {}
        for sid in runnable:
            tf = self._normalize_trigger_tf(self.strategy_trigger_tf.get(sid, "1min"))
            sid_tf[sid] = tf
            if tf not in tf_cache:
                tf_cache[tf] = self._fetch_latest_bar_for_timeframe(tf, current_dt) or base_bar
        by_strategy = {}
        kline_by_strategy = {}
        for sid in runnable:
            tf = sid_tf.get(sid, "1min")
            kline = tf_cache.get(tf, base_bar) or base_bar
            by_strategy[sid] = {
                "current_cash": float(self.revenue.cash),
                "last_price": float(kline.get("close", base_bar.get("close", 0.0)) or 0.0),
                "trigger_timeframe": tf
            }
            kline_by_strategy[sid] = kline
        return {
            "current_cash": float(self.revenue.cash),
            "last_price": float(base_bar.get("close", 0.0)),
            "__by_strategy__": by_strategy,
            "__kline_by_strategy__": kline_by_strategy
        }

    def _fetch_kline_with_provider(self, provider, interval, start_time, end_time):
        self._announce_kline_fetching(timeframe=interval)
        try:
            if hasattr(provider, "fetch_kline_data"):
                df = provider.fetch_kline_data(self.stock_code, start_time, end_time, interval)
                if df is not None and not df.empty:
                    return df
        except Exception:
            pass
        if interval == "1min":
            try:
                df = provider.fetch_minute_data(self.stock_code, start_time, end_time)
                if df is not None and not df.empty:
                    return df
            except Exception:
                return pd.DataFrame()
        return pd.DataFrame()

    def _announce_kline_fetching(self, force=False, timeframe="1min"):
        now_ts = time.time()
        if (not force) and (now_ts - float(self._last_kline_fetch_notice_ts or 0.0) < 8.0):
            return
        self._last_kline_fetch_notice_ts = now_ts
        tf = str(timeframe or "1min")
        tf_text = "日线" if tf.upper() == "D" else tf
        msg = f"当前正在拉取K线数据：{self.stock_code} {tf_text}"
        print(msg)
        if self.event_callback:
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(self._emit_event("system", {"msg": msg, "stock": self.stock_code, "timeframe": tf}))
            except Exception:
                pass

    def _build_bar_from_df_row(self, row):
        if row is None:
            return None
        dt_val = pd.to_datetime(row.get("dt"), errors="coerce")
        if pd.isna(dt_val):
            return None
        bar = {
            "code": str(row.get("code", self.stock_code)),
            "dt": dt_val,
            "open": float(row.get("open", row.get("close", 0.0)) or 0.0),
            "high": float(row.get("high", row.get("close", 0.0)) or 0.0),
            "low": float(row.get("low", row.get("close", 0.0)) or 0.0),
            "close": float(row.get("close", 0.0) or 0.0),
            "vol": float(row.get("vol", row.get("volume", 0.0)) or 0.0),
            "amount": float(row.get("amount", row.get("turnover", 0.0)) or 0.0)
        }
        bar["volume"] = bar["vol"]
        bar["turnover"] = bar["amount"]
        return bar

    def _to_naive_ts(self, dt_value):
        ts = pd.to_datetime(dt_value, errors="coerce")
        if pd.isna(ts):
            return ts
        try:
            tz_obj = getattr(ts, "tz", None)
            if tz_obj is not None:
                try:
                    ts = ts.tz_convert(None)
                except Exception:
                    ts = ts.tz_localize(None)
        except Exception:
            pass
        return ts

    def _prepare_required_kline(self, end_time):
        timeframes = self._required_timeframes()
        span_days = {
            "1min": 8,
            "5min": 20,
            "10min": 30,
            "15min": 30,
            "30min": 45,
            "60min": 90,
            "D": 400
        }
        tf_data = {}
        latest = {}
        for tf in timeframes:
            start_time = end_time - timedelta(days=int(span_days.get(tf, 8)))
            df = self._fetch_kline_with_provider(self.provider, tf, start_time, end_time)
            if (df is None or df.empty) and self._tushare_fallback_provider is not None:
                df = self._fetch_kline_with_provider(self._tushare_fallback_provider, tf, start_time, end_time)
            self._direct_tf_support[tf] = bool(df is not None and not df.empty)
            if df is None or df.empty:
                continue
            df = self.works.clean_data(df)
            if "volume" not in df.columns and "vol" in df.columns:
                df["volume"] = df["vol"]
            if "vol" not in df.columns and "volume" in df.columns:
                df["vol"] = df["volume"]
            if "turnover" not in df.columns and "amount" in df.columns:
                df["turnover"] = df["amount"]
            if "amount" not in df.columns and "turnover" in df.columns:
                df["amount"] = df["turnover"]
            tf_data[tf] = df
            try:
                latest[tf] = pd.to_datetime(df["dt"].max())
            except Exception:
                pass
        self.prepared_kline_latest = {k: str(v) for k, v in latest.items()}
        return tf_data, latest

    def _pull_latest_minute_bar(self):
        end_time = datetime.now()
        start_time = end_time - timedelta(days=3)
        providers = [self.provider]
        if self._tushare_fallback_provider is not None:
            providers.append(self._tushare_fallback_provider)
        for p in providers:
            df = self._fetch_kline_with_provider(p, "1min", start_time, end_time)
            if df is None or df.empty:
                continue
            try:
                row = df.sort_values("dt").iloc[-1].to_dict()
                bar = self._build_bar_from_df_row(row)
                if bar:
                    return bar
            except Exception:
                continue
        return None

    def _expected_latest_trade_date(self, now_dt):
        now_dt = pd.to_datetime(now_dt, errors="coerce")
        if pd.isna(now_dt):
            now_dt = pd.Timestamp(datetime.now())
        d = now_dt.date()
        wd = int(now_dt.weekday())
        if wd >= 5:
            return (now_dt - timedelta(days=wd - 4)).date()
        if int(now_dt.hour) < 9:
            if wd == 0:
                return (now_dt - timedelta(days=3)).date()
            return (now_dt - timedelta(days=1)).date()
        return d

    def _business_days_lag(self, from_date, to_date):
        if from_date is None or to_date is None:
            return 0
        if from_date >= to_date:
            return 0
        try:
            rng = pd.bdate_range(start=from_date, end=to_date)
            return max(0, len(rng) - 1)
        except Exception:
            return max(0, (to_date - from_date).days)

    def _is_trading_day(self, dt_obj):
        dt = pd.to_datetime(dt_obj, errors="coerce")
        if pd.isna(dt):
            return False
        return int(dt.weekday()) < 5

    def _provider_name(self, provider):
        name = str(getattr(provider, "__class__", type("X", (), {})).__name__ or "").lower()
        if "tushare" in name:
            return "tushare"
        if "mysql" in name:
            return "mysql"
        if "postgres" in name:
            return "postgres"
        if "akshare" in name:
            return "akshare"
        if "dataprovider" in name:
            return "default"
        return name or "unknown"

    def _classify_warmup_reason(self, text, provider_name=""):
        t = str(text or "").lower()
        p = str(provider_name or "").lower()
        if ("token" in t) or ("权限" in t) or ("permission" in t) or ("积分" in t):
            return "TOKEN_INVALID", "Token失效/权限不足"
        if ("timeout" in t) or ("timed out" in t) or ("超时" in t):
            return "NETWORK_TIMEOUT", "网络超时"
        if ("connection" in t) or ("连接失败" in t) or ("max retries exceeded" in t) or ("name or service not known" in t) or ("network" in t):
            return "NETWORK_ERROR", "网络连接失败"
        if ("ok_no_data" in t) and (p in {"mysql", "postgres"}):
            return "TABLE_EMPTY", "数据表空/近三日无数据"
        if ("未配置" in t) and ("表名" in t):
            return "TABLE_EMPTY", "数据表未配置"
        if ("无数据" in t) or ("empty" in t) or ("no data" in t) or ("未返回最新行情" in t) or ("历史k线为空" in t):
            return "CODE_NO_DATA", "代码无数据"
        return "UNKNOWN", "未知原因"

    def _startup_failure_context(self):
        providers = [self.provider]
        if self._tushare_fallback_provider is not None:
            providers.append(self._tushare_fallback_provider)
        details = []
        picked_code = ""
        picked_label = ""
        if self.provider_type == "tushare" and (not str(self.tushare_token or "").strip()):
            return "TOKEN_INVALID", "Token未配置", "tushare_token 未配置"
        for provider in providers:
            name = self._provider_name(provider)
            conn_ok = None
            conn_msg = ""
            if hasattr(provider, "check_connectivity"):
                try:
                    ok, msg = provider.check_connectivity(self.stock_code)
                    conn_ok = bool(ok)
                    conn_msg = str(msg or "")
                except Exception as e:
                    conn_ok = False
                    conn_msg = str(e)
            last_err = str(getattr(provider, "last_error", "") or "")
            detail = f"{name}: conn={conn_ok} msg={conn_msg} err={last_err}".strip()
            details.append(detail)
            raw = " | ".join([conn_msg, last_err])
            code, label = self._classify_warmup_reason(raw, provider_name=name)
            if picked_code in {"", "UNKNOWN"} and code != "UNKNOWN":
                picked_code = code
                picked_label = label
        if not picked_code:
            picked_code = "UNKNOWN"
            picked_label = "未知原因"
        detail_text = " || ".join([x for x in details if x]).strip()
        return picked_code, picked_label, detail_text

    def _build_kline_freshness_snapshot(self, latest_map, now_dt=None, check_ok=True, message=""):
        now_dt = pd.to_datetime(now_dt or datetime.now(), errors="coerce")
        expected_date = self._expected_latest_trade_date(now_dt)
        per_tf = []
        has_lag = False
        if isinstance(latest_map, dict):
            for tf, dt_val in sorted(latest_map.items(), key=lambda x: x[0]):
                dt_obj = pd.to_datetime(dt_val, errors="coerce")
                if pd.isna(dt_obj):
                    continue
                latest_date = dt_obj.date()
                lagged = bool(latest_date < expected_date)
                lag_days = self._business_days_lag(latest_date, expected_date)
                has_lag = has_lag or lagged
                per_tf.append({
                    "timeframe": str(tf),
                    "latest_dt": str(dt_obj),
                    "latest_date": latest_date.strftime("%Y-%m-%d"),
                    "expected_latest_trade_date": expected_date.strftime("%Y-%m-%d"),
                    "lagged": lagged,
                    "lag_days": int(lag_days)
                })
        latest_dt_text = ""
        latest_date_text = ""
        if per_tf:
            latest_sorted = sorted(per_tf, key=lambda x: str(x.get("latest_dt", "")))
            latest_dt_text = str(latest_sorted[-1].get("latest_dt", ""))
            latest_date_text = str(latest_sorted[-1].get("latest_date", ""))
        return {
            "stock_code": self.stock_code,
            "check_time": now_dt.strftime("%Y-%m-%d %H:%M:%S"),
            "check_ok": bool(check_ok),
            "message": str(message or ""),
            "reason_category": "OK" if check_ok else "UNKNOWN",
            "reason_label": "正常" if check_ok else "未知原因",
            "reason_detail": "",
            "expected_latest_trade_date": expected_date.strftime("%Y-%m-%d"),
            "latest_kline_date": latest_date_text or "",
            "latest_kline_dt": latest_dt_text or "",
            "lagged": bool(has_lag),
            "timeframes": per_tf
        }

    @staticmethod
    def _prime_strategy_history(strategy, stock_code, df):
        history = getattr(strategy, "history", None)
        if not isinstance(history, dict):
            history = {}
            setattr(strategy, "history", history)
        history[str(stock_code)] = df.copy()

    def warm_up(self):
        """
        Load historical data to initialize strategy indicators.
        """
        print(f"🔥 正在预热策略内阁 (获取 {self.stock_code} 历史数据)...")
        self._announce_kline_fetching(force=True)
        self.startup_kline_freshness = {}
        
        # Get latest time first to know where to look back from
        latest = self.provider.get_latest_bar(self.stock_code)
        if (not latest) and self._tushare_fallback_provider is not None:
            latest = self._tushare_fallback_provider.get_latest_bar(self.stock_code)
        if not latest:
            print("❌ 无法获取最新行情，预热失败。")
            reason_code, reason_label, reason_detail = self._startup_failure_context()
            self.startup_kline_freshness = self._build_kline_freshness_snapshot(
                latest_map={},
                now_dt=datetime.now(),
                check_ok=False,
                message="无法获取最新行情"
            )
            self.startup_kline_freshness["reason_category"] = reason_code
            self.startup_kline_freshness["reason_label"] = reason_label
            self.startup_kline_freshness["reason_detail"] = reason_detail
            return False
            
        end_time = latest['dt']
        tf_data, latest_map = self._prepare_required_kline(end_time)
        df = tf_data.get("1min")
        if df is None or df.empty:
            start_time = end_time - timedelta(days=5)
            df = self.provider.fetch_minute_data(self.stock_code, start_time, end_time)
            if (df is None or df.empty) and self._tushare_fallback_provider is not None:
                df = self._tushare_fallback_provider.fetch_minute_data(self.stock_code, start_time, end_time)
        if df.empty:
            print("❌ 历史数据为空，策略无法初始化。")
            reason_code, reason_label, reason_detail = self._startup_failure_context()
            if reason_code == "UNKNOWN":
                reason_code, reason_label = "CODE_NO_DATA", "代码无数据"
            self.startup_kline_freshness = self._build_kline_freshness_snapshot(
                latest_map=latest_map,
                now_dt=datetime.now(),
                check_ok=False,
                message="历史K线为空"
            )
            self.startup_kline_freshness["reason_category"] = reason_code
            self.startup_kline_freshness["reason_label"] = reason_label
            self.startup_kline_freshness["reason_detail"] = reason_detail
            return False
            
        df = self.works.clean_data(df)
        if "volume" not in df.columns and "vol" in df.columns:
            df["volume"] = df["vol"]
        if "vol" not in df.columns and "volume" in df.columns:
            df["vol"] = df["volume"]
        if "turnover" not in df.columns and "amount" in df.columns:
            df["turnover"] = df["amount"]
        if "amount" not in df.columns and "turnover" in df.columns:
            df["amount"] = df["turnover"]
        print(f"✅ 获取到 {len(df)} 条历史K线，正在回放以初始化策略状态...")
        active_set = {str(x) for x in (self.active_strategy_ids or [])}
        strategies_to_warm = [s for s in self.strategies if (not active_set) or (str(s.id) in active_set)]
        if not strategies_to_warm:
            print("⚠️ 未匹配到可用策略，预热跳过。")
            return True
        last_bar = df.iloc[-1]
        for strategy in strategies_to_warm:
            self._prime_strategy_history(strategy, self.stock_code, df)
            try:
                strategy.on_bar(last_bar)
            except Exception as e:
                print(f"⚠️ 策略预热失败 id={strategy.id} err={e}")
            
        self.last_dt = df.iloc[-1]['dt']
        try:
            preload = df[["dt", "code", "open", "high", "low", "close", "vol", "amount"]].tail(5000).copy()
            preload["dt"] = pd.to_datetime(preload["dt"], errors="coerce")
            preload = preload.dropna(subset=["dt"])
            self.daily_data_buffer = [row for row in preload.to_dict("records")]
        except Exception:
            pass
        latest_text = ", ".join([f"{k}:{v}" for k, v in sorted(latest_map.items(), key=lambda x: x[0])]) if latest_map else "无"
        self.startup_kline_freshness = self._build_kline_freshness_snapshot(
            latest_map=latest_map,
            now_dt=datetime.now(),
            check_ok=True,
            message="启动校验完成"
        )
        print(f"✅ 预热完成。最新数据时间: {self.last_dt} | 周期最新点: {latest_text}")
        return True

    async def _emit_event(self, event_type, data):
        """
        Emit event to frontend via callback (async).
        """
        if self.event_callback:
            await self.event_callback(event_type, data)

    def _live_cfg(self, path, default=None):
        cfg = ConfigLoader.reload()
        return cfg.get(path, default)

    def _sync_realtime_bar_to_default(self, bar):
        if not self._rt_sync_to_default_enabled:
            return
        if not isinstance(bar, dict):
            return
        provider = self._rt_sync_to_default_provider
        if provider is None:
            return
        dt_val = pd.to_datetime(bar.get("dt"), errors="coerce")
        if pd.isna(dt_val):
            return
        code = str(bar.get("code", self.stock_code) or self.stock_code).upper()
        bar_key = f"{code}|{dt_val.strftime('%Y-%m-%d %H:%M:%S')}"
        if bar_key in self._rt_synced_bar_keys:
            return
        rowcount = int(provider.upsert_realtime_minute_bar(bar, on_duplicate=self._rt_sync_to_default_on_duplicate) or 0)
        if rowcount > 0:
            self._rt_synced_bar_keys.add(bar_key)
            if len(self._rt_synced_bar_keys) > 20000:
                self._rt_synced_bar_keys = set(list(self._rt_synced_bar_keys)[-5000:])
            self._rt_sync_last_error = ""
            return
        err = str(getattr(provider, "last_error", "") or "default datasource write failed")
        if err != self._rt_sync_last_error:
            print(f"⚠️ 实时增量回写 default 失败: {err}")
            self._rt_sync_last_error = err

    def _level_of(self, value, warn, critical):
        v = float(value or 0.0)
        w = float(warn or 0.0)
        c = float(critical or 0.0)
        if c > 0 and v >= c:
            return "critical"
        if w > 0 and v >= w:
            return "warn"
        return "ok"

    async def _emit_live_alert(self, metric, value, warn, critical, current_dt, extra=None):
        level = self._level_of(value, warn, critical)
        prev = self._live_alert_last.get(metric)
        self._live_alert_last[metric] = level
        if level == "ok":
            return
        if prev == level:
            return
        payload = {
            "time": current_dt.strftime("%H:%M:%S"),
            "metric": metric,
            "level": level,
            "value": float(value),
            "warn": float(warn),
            "critical": float(critical)
        }
        if isinstance(extra, dict):
            payload.update(extra)
        await self._emit_event("live_alert", payload)
        badge = "bg-trading-yellow" if level == "warn" else "bg-trading-red"
        await self._emit_event("menxia", {
            "msg": f"实盘监控告警: {metric}={float(value):.6f}",
            "details": f"> 等级: {level}<br>> 当前值: {float(value):.6f}<br>> warn: {float(warn):.6f}<br>> critical: {float(critical):.6f}",
            "status": badge,
            "decision": "alert",
            "metric": metric,
            "level": level
        })

    def _today_turnover_ratio(self, current_dt, fund_value_now):
        base_fund_value = float(self._day_start_fund_value or 0.0)
        if base_fund_value <= 0:
            base_fund_value = float(fund_value_now or 0.0)
        if base_fund_value <= 0:
            return 0.0
        buy_total = 0.0
        sell_total = 0.0
        for tx in self.revenue.transactions:
            dt_val = pd.to_datetime(tx.get("dt"), errors="coerce")
            if pd.isna(dt_val):
                continue
            if dt_val.date() != current_dt.date():
                continue
            amount = abs(float(tx.get("amount", 0.0) or 0.0))
            direction = str(tx.get("direction", "")).upper()
            if direction == "SELL":
                sell_total += amount
            elif direction == "BUY":
                buy_total += amount
        effective_turnover = max(buy_total, sell_total)
        return effective_turnover / base_fund_value

    def _live_lot_snapshot(self, current_dt):
        curr_day = pd.to_datetime(current_dt, errors="coerce")
        curr_day_text = "" if pd.isna(curr_day) else curr_day.strftime("%Y-%m-%d")
        target_code = str(self.stock_code or "").upper()
        rows = []
        total_qty = 0
        sellable_qty = 0
        for strategy_id, stocks in self.state_affairs.positions.items():
            if not isinstance(stocks, dict):
                continue
            for code, pos in stocks.items():
                code_text = str(code or "").upper()
                if code_text != target_code:
                    continue
                lots = pos.get("lots")
                if not isinstance(lots, list) or not lots:
                    qty_legacy = int(pos.get("qty", 0) or 0)
                    if qty_legacy <= 0:
                        continue
                    lots = [{
                        "qty": qty_legacy,
                        "buy_day": str(pos.get("last_buy_day", "")).strip(),
                        "unit_cost": float(pos.get("avg_price", 0.0) or 0.0)
                    }]
                for lot in lots:
                    lot_qty = int(lot.get("qty", 0) or 0)
                    if lot_qty <= 0:
                        continue
                    buy_day = str(lot.get("buy_day", "")).strip()
                    can_sell = lot_qty if (buy_day and buy_day != curr_day_text) else 0
                    total_qty += lot_qty
                    sellable_qty += can_sell
                    rows.append({
                        "strategy_id": str(strategy_id),
                        "code": code_text,
                        "buy_day": buy_day or "--",
                        "qty": lot_qty,
                        "sellable_qty": can_sell,
                        "locked_qty": lot_qty - can_sell,
                        "unit_cost": float(lot.get("unit_cost", 0.0) or 0.0)
                    })
        rows.sort(key=lambda x: (x["strategy_id"], x["buy_day"], x["qty"]))
        return {
            "time": curr_day.strftime("%H:%M:%S") if not pd.isna(curr_day) else "--",
            "total_qty": int(total_qty),
            "sellable_qty": int(sellable_qty),
            "locked_qty": int(max(0, total_qty - sellable_qty)),
            "rows": rows
        }

    def _ensure_daily_summary_state(self, current_dt, fund_value_now=None):
        dt_obj = pd.to_datetime(current_dt, errors="coerce")
        if pd.isna(dt_obj):
            return
        day_text = dt_obj.strftime("%Y-%m-%d")
        if day_text != self._summary_day:
            self._summary_day = day_text
            self._day_start_fund_value = float(fund_value_now) if fund_value_now is not None else None
            self._intraday_fund_curve = []
            self._intraday_signal_counter = {}
            self._daily_summary_sent_day = ""
        if self._day_start_fund_value is None and fund_value_now is not None:
            self._day_start_fund_value = float(fund_value_now)
        if fund_value_now is not None:
            self._intraday_fund_curve.append(float(fund_value_now))

    def _build_daily_summary_payload(self, current_dt):
        dt_obj = pd.to_datetime(current_dt, errors="coerce")
        if pd.isna(dt_obj):
            dt_obj = pd.to_datetime(datetime.now(), errors="coerce")
        day_text = dt_obj.strftime("%Y-%m-%d")
        tx_today = []
        for tx in self.revenue.transactions:
            tx_dt = pd.to_datetime(tx.get("dt"), errors="coerce")
            if pd.isna(tx_dt):
                continue
            if tx_dt.strftime("%Y-%m-%d") != day_text:
                continue
            tx_today.append(tx)
        total_trades = len(tx_today)
        sell_tx = [x for x in tx_today if str(x.get("direction", "")).upper() == "SELL"]
        win_sell = [x for x in sell_tx if float(x.get("pnl", 0.0) or 0.0) > 0]
        win_rate = (len(win_sell) / len(sell_tx) * 100.0) if sell_tx else 0.0
        realized_pnl = float(sum([float(x.get("pnl", 0.0) or 0.0) for x in sell_tx]))
        curve = [float(x) for x in self._intraday_fund_curve if x is not None]
        if not curve and self._day_start_fund_value is not None:
            curve = [float(self._day_start_fund_value)]
        start_value = float(self._day_start_fund_value) if self._day_start_fund_value is not None else (curve[0] if curve else 0.0)
        end_value = curve[-1] if curve else start_value
        net_pnl = float(end_value - start_value)
        peak = curve[0] if curve else 0.0
        max_dd = 0.0
        for v in curve:
            peak = max(float(peak), float(v))
            if float(peak) > 0:
                max_dd = max(max_dd, (float(peak) - float(v)) / float(peak))
        top3 = sorted(self._intraday_signal_counter.items(), key=lambda x: (-int(x[1]), str(x[0])))[:3]
        top3_list = [{"name": str(k), "count": int(v)} for k, v in top3]
        return {
            "time": dt_obj.strftime("%H:%M:%S"),
            "date": day_text,
            "event_type": "daily_summary",
            "total_trades": int(total_trades),
            "win_rate": round(float(win_rate), 2),
            "realized_pnl": round(float(realized_pnl), 2),
            "net_pnl": round(float(net_pnl), 2),
            "max_drawdown": round(float(max_dd), 6),
            "start_fund_value": round(float(start_value), 2),
            "end_fund_value": round(float(end_value), 2),
            "top3_signals": top3_list
        }

    async def _emit_daily_summary_if_needed(self, current_dt):
        dt_obj = pd.to_datetime(current_dt, errors="coerce")
        if pd.isna(dt_obj):
            return
        day_text = dt_obj.strftime("%Y-%m-%d")
        if self._daily_summary_sent_day == day_text:
            return
        payload = self._build_daily_summary_payload(dt_obj)
        self._daily_summary_sent_day = day_text
        await self._emit_event("daily_summary", payload)

    async def _check_live_monitor_alerts(self, current_dt, fund_value_now, holdings_value_now, api_latency_ms, signal_consistency):
        dd_warn = float(self._live_cfg("live_monitoring.risk_alerts.daily_drawdown_warn", 0.02))
        dd_critical = float(self._live_cfg("live_monitoring.risk_alerts.daily_drawdown_critical", 0.03))
        pos_warn = float(self._live_cfg("live_monitoring.risk_alerts.single_position_weight_warn", 0.15))
        pos_critical = float(self._live_cfg("live_monitoring.risk_alerts.single_position_weight_critical", 0.2))
        turnover_warn = float(self._live_cfg("live_monitoring.risk_alerts.turnover_rate_warn", 0.5))
        turnover_critical = float(self._live_cfg("live_monitoring.risk_alerts.turnover_rate_critical", 0.8))
        delay_warn_s = float(self._live_cfg("live_monitoring.consistency_checks.signal_execution_delay_seconds_warn", 5))
        delay_critical_s = float(self._live_cfg("live_monitoring.consistency_checks.signal_execution_delay_seconds_critical", 15))
        delay_warn_ms = delay_warn_s * 1000.0
        delay_critical_ms = delay_critical_s * 1000.0
        self.peak_fund_value = max(float(self.peak_fund_value), float(fund_value_now))
        curve = [float(x) for x in self._intraday_fund_curve if x is not None]
        daily_peak = max(curve) if curve else float(fund_value_now)
        drawdown = 0.0
        if daily_peak > 0:
            drawdown = max(0.0, (daily_peak - float(fund_value_now)) / daily_peak)
        pos_weight = 0.0
        if float(fund_value_now) > 0:
            pos_weight = float(holdings_value_now) / float(fund_value_now)
        turnover_ratio = self._today_turnover_ratio(current_dt, fund_value_now)
        await self._emit_live_alert("daily_drawdown", drawdown, dd_warn, dd_critical, current_dt)
        await self._emit_live_alert("single_position_weight", pos_weight, pos_warn, pos_critical, current_dt)
        await self._emit_live_alert("turnover_rate", turnover_ratio, turnover_warn, turnover_critical, current_dt)
        await self._emit_live_alert("signal_execution_delay_ms", float(api_latency_ms), delay_warn_ms, delay_critical_ms, current_dt)
        await self._emit_event("live_monitor_snapshot", {
            "time": current_dt.strftime("%H:%M:%S"),
            "daily_drawdown": round(drawdown, 6),
            "single_position_weight": round(pos_weight, 6),
            "turnover_rate": round(turnover_ratio, 6),
            "signal_consistency": round(float(signal_consistency), 2),
            "api_latency_ms": int(api_latency_ms)
        })

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
        await self._emit_account_snapshot()
        await self._emit_event('fund_pool', self.get_fund_pool_snapshot(include_transactions=False))
        
        # Try warm up, if it fails report reason and stop live loop
        warmup_success = await asyncio.to_thread(self.warm_up)
        if self.startup_kline_freshness:
            await self._emit_event("live_kline_freshness", self.startup_kline_freshness)
        
        print(f"\n🚀 【三省六部】实时监控已启动")
        print(f"👁️  监控标的: {self.stock_code}")
        print(f"🛡️  运行策略: 激活 {len(self.active_strategy_ids)} / 总计 {len(self.strategies)} 套")
        if not warmup_success:
            reason = str(self.startup_kline_freshness.get("message", "启动校验失败") or "启动校验失败")
            reason_category = str(self.startup_kline_freshness.get("reason_category", "UNKNOWN") or "UNKNOWN")
            reason_label = str(self.startup_kline_freshness.get("reason_label", "未知原因") or "未知原因")
            reason_detail = str(self.startup_kline_freshness.get("reason_detail", "") or "")
            fail_msg = f"启动校验失败（{reason_label}）：{reason}"
            if reason_detail:
                fail_msg = f"{fail_msg} | {reason_detail}"
            print(f"❌ {fail_msg}")
            await self._emit_event('system', {
                'msg': fail_msg,
                'stock': self.stock_code,
                'reason_category': reason_category,
                'reason_label': reason_label,
                'reason_detail': reason_detail
            })
            raise RuntimeError(fail_msg)
        print("--------------------------------------------------")
        active_ids = [str(x) for x in (self.active_strategy_ids or []) if str(x)]
        active_names = []
        for sid in active_ids:
            st = self.personnel.get_strategy(sid)
            active_names.append(st.name if st else sid)
        await self._emit_event('system', {
            'msg': f"当前实盘已启动：标的 {self.stock_code}，策略 {('、'.join(active_names) if active_names else '全部')}",
            'stock': self.stock_code,
            'strategy_ids': active_ids,
            'strategy_names': active_names
        })
        
        # Initial Event
        await self._emit_event('system', {'msg': '内阁实时监控已启动', 'stock': self.stock_code})
        
        try:
            while True:
                try:
                    should_stop = await self._tick()
                    if should_stop:
                        break
                except Exception as e:
                    print(f"❌ 实盘tick异常: {e}")
                    await self._emit_event('system', {'msg': f'实盘tick异常: {e}', 'stock': self.stock_code})
                replay_speed = float(os.getenv("OPENCLAW_TUSHARE_REPLAY_SPEED", "0") or 0.0)
                if str(self.provider_type).lower() == "tushare" and replay_speed > 0:
                    sleep_seconds = 1
                else:
                    sleep_seconds = 7 if str(self.provider_type).lower() == "tushare" else 3
                await asyncio.sleep(sleep_seconds)
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
                self.active_strategy_ids = [s.id for s in self.strategies if str(s.id) in picked]
        elif strategy_id == 'all':
            self.active_strategy_ids = [s.id for s in self.strategies]
        else:
            self.active_strategy_ids = [strategy_id]
        self.strategy_trigger_tf = {s.id: self._normalize_trigger_tf(getattr(s, "trigger_timeframe", "1min")) for s in self.strategies}
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

    def _is_market_session_time(self, dt_obj):
        dt = pd.to_datetime(dt_obj, errors="coerce")
        if pd.isna(dt):
            return False
        if not self._is_trading_day(dt):
            return False
        hhmm = int(dt.hour) * 100 + int(dt.minute)
        in_morning = 930 <= hhmm < 1130
        in_afternoon = 1300 <= hhmm < 1500
        return in_morning or in_afternoon

    def _get_runnable_strategy_ids(self, current_dt):
        runnable = []
        for sid in self.active_strategy_ids:
            if sid is None:
                continue
            tf = self._normalize_trigger_tf(self.strategy_trigger_tf.get(sid, "1min"))
            if self._is_timeframe_tick(current_dt, tf):
                runnable.append(sid)
        return runnable

    def _format_tick_trigger_log(self, runnable_strategy_ids):
        grouped = {}
        for sid in runnable_strategy_ids:
            if sid is None:
                continue
            tf = self._normalize_trigger_tf(self.strategy_trigger_tf.get(sid, "1min"))
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

    async def _tick(self):
        current_dt = None
        bar = None
        api_latency_ms = 0
        now_wall = datetime.now()
        should_stop = await self._check_market_close(now_wall)
        if should_stop:
            return True

        self._announce_kline_fetching(timeframe="1min")
        t0 = time.perf_counter()
        bar = await asyncio.to_thread(self.provider.get_latest_bar, self.stock_code)
        api_latency_ms = int((time.perf_counter() - t0) * 1000)
        if (not bar) and self._tushare_fallback_provider is not None:
            t0 = time.perf_counter()
            bar = await asyncio.to_thread(self._tushare_fallback_provider.get_latest_bar, self.stock_code)
            api_latency_ms = int((time.perf_counter() - t0) * 1000)
        if not bar:
            repaired = await asyncio.to_thread(self._pull_latest_minute_bar)
            if repaired is not None:
                bar = repaired
            else:
                reason_code, reason_label, reason_detail = self._startup_failure_context()
                reason_text = reason_detail if reason_detail else "未返回最新行情"
                fail_msg = f"K线拉取失败（{reason_label}）：{reason_text}"
                await self._emit_event('system', {
                    'msg': fail_msg,
                    'stock': self.stock_code,
                    'reason_category': reason_code,
                    'reason_label': reason_label,
                    'reason_detail': reason_detail
                })
                raise RuntimeError(fail_msg)

        current_dt = self._to_naive_ts(bar.get('dt'))
        self.last_dt = self._to_naive_ts(self.last_dt)
        if self.last_dt is not None and (not pd.isna(self.last_dt)) and current_dt <= self.last_dt:
            repaired = await asyncio.to_thread(self._pull_latest_minute_bar)
            if repaired is not None:
                repaired_dt = self._to_naive_ts(repaired.get('dt'))
                if repaired_dt > self.last_dt:
                    bar = repaired
                    current_dt = repaired_dt
            if self.last_dt is not None and (not pd.isna(self.last_dt)) and current_dt <= self.last_dt:
                if (datetime.now() - current_dt) > timedelta(days=1):
                    print(f"⚠️ 最新K线时间疑似过旧: {current_dt}")
                print(f"⏳ 等待K线更新... (当前: {current_dt})", end='\r')
            return False
        self.last_dt = current_dt
        now_wall = datetime.now()
        if current_dt is not None and current_dt > (now_wall + timedelta(minutes=1)):
            current_dt = now_wall.replace(second=0, microsecond=0)
            bar['dt'] = current_dt
        if not self._is_market_session_time(current_dt):
            return False
        if "volume" not in bar and "vol" in bar:
            bar["volume"] = bar["vol"]
        if "vol" not in bar and "volume" in bar:
            bar["vol"] = bar["volume"]
        if "turnover" not in bar and "amount" in bar:
            bar["turnover"] = bar["amount"]
        if "amount" not in bar and "turnover" in bar:
            bar["amount"] = bar["turnover"]
        await asyncio.to_thread(self._sync_realtime_bar_to_default, bar)

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
            'kline_dt': str(current_dt),
            'stock_code': str(bar.get('code') or self.stock_code or '').upper()
        })
        holdings_value_now = self.state_affairs.update_holdings_value({bar['code']: bar['close']})
        fund_value_now = float(self.revenue.cash) + float(holdings_value_now)
        self._ensure_daily_summary_state(current_dt, fund_value_now)
        await self._emit_account_snapshot(holdings_value_now)
        await self._emit_event('fund_pool', self.get_fund_pool_snapshot(include_transactions=False))

        print(f"\n🆕 新K线生成: {current_dt} | Close: {bar['close']:.2f}")
        
        runnable_strategy_ids = self._get_runnable_strategy_ids(current_dt)
        tf_text, name_text = self._format_tick_trigger_log(runnable_strategy_ids)
        print(f"   ⏱ 本tick触发周期与策略: {tf_text if tf_text else '无'}")
        print(f"   📋 本tick策略列表: {name_text if name_text else '无'}")
        strategy_context = self._build_strategy_context(current_dt, bar, runnable_strategy_ids)
        signals = self.secretariat.generate_signals(
            bar,
            runnable_strategy_ids=runnable_strategy_ids,
            strategy_context=strategy_context
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
        await self._check_live_monitor_alerts(
            current_dt=current_dt,
            fund_value_now=fund_value_now,
            holdings_value_now=holdings_value_now,
            api_latency_ms=api_latency_ms,
            signal_consistency=signal_consistency
        )
        
        if not active_signals:
            print("   💤 无策略信号")
        
        for signal in active_signals:
            strategy_id = signal['strategy_id']
            strategy_name = self.personnel.get_strategy(strategy_id).name
            key = f"{strategy_id}-{strategy_name}"
            self._intraday_signal_counter[key] = int(self._intraday_signal_counter.get(key, 0) or 0) + 1
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
            self.peak_fund_value = max(float(self.peak_fund_value), float(current_fund_value))
            current_drawdown = 0.0
            if self.peak_fund_value > 0:
                current_drawdown = max(0.0, (float(self.peak_fund_value) - float(current_fund_value)) / float(self.peak_fund_value))
            approved, reason = self.chancellery.check_signal(signal, current_fund_value, current_positions, 0.0, current_drawdown=current_drawdown)
            
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
                    current_position_amount = float(new_qty) * float(bar.get('close', 0.0) or 0.0)
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
                        'realized_pnl': realized_pnl,
                        'expected_price': float(signal.get('price', 0.0)),
                        'actual_price': float(bar.get('close', 0.0)),
                        'current_position_qty': int(new_qty),
                        'current_position_amount': float(current_position_amount)
                    })
                    self._persist_virtual_fund_pool()
                    await self._emit_event('fund_pool', self.get_fund_pool_snapshot(include_transactions=False))
                    expected_price = float(signal.get('price', 0.0) or 0.0)
                    actual_price = float(bar.get('close', 0.0) or 0.0)
                    deviation = 0.0 if expected_price <= 0 else abs(actual_price - expected_price) / expected_price
                    dev_warn = float(self._live_cfg("live_monitoring.consistency_checks.expected_vs_actual_price_deviation_warn", 0.003))
                    dev_critical = float(self._live_cfg("live_monitoring.consistency_checks.expected_vs_actual_price_deviation_critical", 0.008))
                    await self._emit_live_alert(
                        "expected_vs_actual_price_deviation",
                        deviation,
                        dev_warn,
                        dev_critical,
                        current_dt,
                        extra={"strategy_id": strategy_id}
                    )
                    
                    await self._emit_event('shangshu', {
                    'msg': f"执行成功! 持仓: {new_qty}",
                    'details': f"> 成交价: {bar['close']:.2f}<br>> 新持仓: {new_qty}",
                    'status': 'bg-trading-green'
                })
                    
                    await self._emit_account_snapshot()
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
            remaining_qty = self.state_affairs.positions.get(order['strategy_id'], {}).get(order['code'], {}).get('qty', 0)
            current_position_amount = float(remaining_qty) * float(bar.get('close', 0.0) or 0.0)
            self.secretariat.update_strategy_state(order['strategy_id'], order['code'], remaining_qty)
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
                'realized_pnl': realized_pnl,
                'current_position_qty': int(remaining_qty),
                'current_position_amount': float(current_position_amount)
            })
            self._persist_virtual_fund_pool()
            await self._emit_event('fund_pool', self.get_fund_pool_snapshot(include_transactions=False))
            await self._emit_account_snapshot()
        await self._emit_event('live_position_lots', self._live_lot_snapshot(current_dt))
        return False

    async def _check_market_close(self, current_dt):
        dt_obj = pd.to_datetime(current_dt, errors="coerce")
        if pd.isna(dt_obj):
            return False
        day_text = dt_obj.strftime("%Y-%m-%d")
        last_dt = pd.to_datetime(self._last_summary_tick_dt, errors="coerce") if self._last_summary_tick_dt is not None else pd.NaT
        self._last_summary_tick_dt = dt_obj

        summary_cutoff = dt_obj.replace(hour=15, minute=5, second=0, microsecond=0)
        stop_cutoff = dt_obj.replace(hour=15, minute=30, second=0, microsecond=0)
        reached_summary_cutoff = dt_obj >= summary_cutoff and (pd.isna(last_dt) or last_dt < summary_cutoff)
        reached_stop_cutoff = dt_obj >= stop_cutoff and (pd.isna(last_dt) or last_dt < stop_cutoff)

        should_summary = self._is_trading_day(dt_obj) and reached_summary_cutoff and (self._daily_summary_sent_day != day_text)
        if should_summary:
            print(f"🌆 日终汇总时间到 ({dt_obj})，正在归档今日数据...")
            await self._emit_daily_summary_if_needed(dt_obj)
            self._archive_data()
            self.today_archived = True

        should_auto_stop = self._is_trading_day(dt_obj) and reached_stop_cutoff and (self._daily_auto_stop_day != day_text)
        if should_auto_stop:
            self._daily_auto_stop_day = day_text
            stop_msg = f"已超过15:30，自动关闭实盘模式：{day_text}"
            print(f"🛑 {stop_msg}")
            await self._emit_event("system", {
                "msg": stop_msg,
                "stock": self.stock_code,
                "reason": "after_1530"
            })
            await self._emit_event("live_auto_stop", {
                "msg": stop_msg,
                "reason": "after_1530",
                "date": day_text,
                "time": dt_obj.strftime("%H:%M:%S")
            })
            return True

        if dt_obj.hour < 9:
            self.today_archived = False
            if self._daily_auto_stop_day != day_text:
                self._daily_auto_stop_day = ""
        return False

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
