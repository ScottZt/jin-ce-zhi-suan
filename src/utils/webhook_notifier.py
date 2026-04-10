import asyncio
import base64
import hashlib
import hmac
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from src.utils.config_loader import ConfigLoader


logger = logging.getLogger("WebhookNotifier")
FEISHU_LIVE_DISCLAIMER = "本信号为算法计算，非投资建议，仅供研究，不构成买卖依据。"


class WebhookNotifier:
    def __init__(self):
        self._last_sent = {}
        self._cfg_cache = {}
        self._cfg_cache_at = 0.0
        self._failed_lock = threading.Lock()
        self._retry_lock = asyncio.Lock()
        self._last_retry_ts = 0.0
        self._strategy_name_map_cache = {}
        self._strategy_name_map_at = 0.0

    def _load_cfg(self):
        now = time.time()
        if (now - self._cfg_cache_at) < 3 and isinstance(self._cfg_cache, dict):
            return self._cfg_cache
        cfg = ConfigLoader.reload()
        section = cfg.get("webhook_notification", {})
        section = section if isinstance(section, dict) else {}
        self._cfg_cache = section
        self._cfg_cache_at = now
        return section

    def _fingerprint(self, event_type, stock_code, data):
        if isinstance(data, dict):
            focus = {
                "strategy_id": data.get("strategy_id"),
                "direction": data.get("direction"),
                "price": data.get("price"),
                "qty": data.get("qty"),
                "level": data.get("level"),
                "metric": data.get("metric"),
                "time": data.get("time"),
                "msg": data.get("msg"),
            }
        else:
            focus = {"value": str(data)}
        raw = json.dumps(
            {"event_type": event_type, "stock_code": stock_code, "focus": focus},
            ensure_ascii=False,
            sort_keys=True
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _should_send(self, event_type, stock_code, data, dedupe_window_seconds):
        fp = self._fingerprint(event_type, stock_code, data)
        now = time.time()
        last_ts = float(self._last_sent.get(fp, 0.0) or 0.0)
        if (now - last_ts) < float(dedupe_window_seconds):
            return False
        self._last_sent[fp] = now
        if len(self._last_sent) > 20000:
            keys = list(self._last_sent.keys())
            for k in keys[:5000]:
                self._last_sent.pop(k, None)
        return True

    def _build_text(self, event_type, stock_code, data):
        ts_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        evt_label_map = {
            "trade_exec": "成交信号",
            "live_alert": "实盘告警",
            "zhongshu": "策略信号",
            "menxia": "风控结果",
            "daily_summary": "日终汇总",
            "system": "系统消息"
        }
        dir_label_map = {
            "BUY": "买入",
            "SELL": "卖出"
        }
        level_label_map = {
            "ok": "正常",
            "warn": "预警",
            "critical": "严重"
        }
        metric_label_map = {
            "daily_drawdown": "当日回撤",
            "single_position_weight": "单票仓位",
            "turnover_rate": "当日换手率",
            "signal_execution_delay_ms": "执行延迟",
            "expected_vs_actual_price_deviation": "成交偏差"
        }
        event_label = evt_label_map.get(str(event_type or ""), str(event_type or ""))
        if not isinstance(data, dict):
            return "\n".join([
                "交易信号通知",
                f"时间: {ts_text}",
                f"标的: {stock_code}",
                f"事件: {event_label}",
                f"内容: {str(data)}"
            ])
        strategy_id = str(data.get("strategy_id", "") or "")
        strategy_name_raw = str(data.get("strategy_name", "") or "").strip()
        strategy_name = ""
        if strategy_name_raw and (not self._looks_garbled(strategy_name_raw)):
            strategy_name = self._normalize_strategy_display_name(strategy_name_raw)
        elif strategy_id:
            mapped_name = str(self._load_strategy_name_map().get(strategy_id, "") or "").strip()
            if mapped_name and (not self._looks_garbled(mapped_name)):
                strategy_name = f"{strategy_id}-{mapped_name}"
        if (not strategy_name) and strategy_id:
            strategy_name = self._normalize_strategy_display_name(strategy_id)
        direction = str(data.get("direction", "") or "").upper()
        direction_label = dir_label_map.get(direction, direction)
        qty_val = self._to_float(data.get("qty", None))
        hands_val = None if qty_val is None else (qty_val / 100.0)
        price_val = self._to_float(data.get("price", None))
        amount_val = self._to_float(data.get("amount", None))
        if amount_val is None and (price_val is not None) and (qty_val is not None):
            amount_val = float(price_val) * float(qty_val)
        fee_est = self._estimate_fee(direction, amount_val)
        level = str(data.get("level", "") or "").lower()
        level_label = level_label_map.get(level, "")
        metric = str(data.get("metric", "") or "")
        metric_label = metric_label_map.get(metric, metric)
        msg = str(data.get("msg", "") or "").strip()
        msg_garbled = ("�" in msg) or (msg.count("?") >= 2)
        if msg_garbled:
            if event_type == "zhongshu":
                msg = f"策略[{strategy_id or '--'}]生成信号: {direction_label or '--'} @ {data.get('price', '--')}"
            elif event_type == "menxia":
                msg = str(data.get("decision", "") or "").lower()
                msg = "风控审核通过" if msg == "approved" else ("风控审核拒绝" if msg == "rejected" else "风控状态更新")
            elif event_type == "trade_exec":
                msg = f"执行{direction_label or '--'}成交 @ {data.get('price', '--')}"
            elif event_type == "live_alert":
                msg = f"{metric_label or '指标'}触发{level_label or '告警'}"
        lines = [
            "交易信号通知",
            f"时间: {ts_text}",
            f"标的: {stock_code}",
            f"事件: {event_label}"
        ]
        event_time = str(data.get("time", "") or "").strip()
        if event_time:
            lines.append(f"信号时间: {event_time}")
        if strategy_id:
            lines.append(f"策略ID: {strategy_id}")
        if strategy_name:
            lines.append(f"策略名称: {strategy_name}")
        if direction_label:
            lines.append(f"方向: {direction_label}")
        if data.get("price") is not None:
            lines.append(f"价格: {data.get('price')}")
        if data.get("qty") is not None:
            lines.append(f"数量: {data.get('qty')}")
        if hands_val is not None:
            lines.append(f"手数: {hands_val:.2f}手")
        if amount_val is not None:
            amount_label = "买入金额" if direction == "BUY" else ("卖出金额" if direction == "SELL" else "成交金额")
            lines.append(f"{amount_label}: {self._fmt_amount(amount_val)}")
        if fee_est is not None:
            lines.append(f"预估手续费: {self._fmt_amount(fee_est.get('total'))}")
        if data.get("current_position_qty") is not None:
            lines.append(f"当前持仓数量: {data.get('current_position_qty')}")
        if data.get("current_position_amount") is not None:
            lines.append(f"当前持仓金额: {data.get('current_position_amount')}")
        if metric_label:
            lines.append(f"监控指标: {metric_label}")
        if level_label:
            lines.append(f"告警级别: {level_label}")
        if msg:
            lines.append(f"说明: {msg}")
        if data.get("warn") is not None:
            lines.append(f"预警阈值: {data.get('warn')}")
        if data.get("critical") is not None:
            lines.append(f"严重阈值: {data.get('critical')}")
        if event_type == "daily_summary":
            if data.get("total_trades") is not None:
                lines.append(f"总交易数: {data.get('total_trades')}")
            if data.get("win_rate") is not None:
                lines.append(f"胜率: {data.get('win_rate')}%")
            if data.get("net_pnl") is not None:
                lines.append(f"当日净盈亏: {data.get('net_pnl')}")
            if data.get("max_drawdown") is not None:
                dd_pct = float(data.get("max_drawdown", 0.0) or 0.0) * 100.0
                lines.append(f"最大回撤: {dd_pct:.2f}%")
            top3 = data.get("top3_signals", [])
            if isinstance(top3, list) and top3:
                top_text = []
                for i, item in enumerate(top3[:3], start=1):
                    if isinstance(item, dict):
                        name = self._normalize_strategy_display_name(item.get("name", "--"), rank_idx=i)
                        cnt = int(item.get("count", 0) or 0)
                        top_text.append(f"{name}({cnt})")
                if top_text:
                    lines.append(f"Top3信号: {' / '.join(top_text)}")
        lines = [
            x for x in lines if str(x or "").strip()
        ]
        return "\n".join(lines)

    def _allow_feishu_event(self, event_type, data):
        et = str(event_type or "").strip()
        if et == "daily_summary":
            return True
        if et == "system":
            return True
        if et != "trade_exec":
            return False
        if not isinstance(data, dict):
            return False
        direction = str(data.get("direction", "") or "").upper()
        return direction in {"BUY", "SELL"}

    def _feishu_sign(self, secret):
        ts = str(int(time.time()))
        string_to_sign = f"{ts}\n{secret}"
        digest = hmac.new(secret.encode("utf-8"), string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(digest).decode("utf-8")
        return ts, sign

    def _post_json(self, url, payload, timeout_sec):
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
            if not raw:
                return {}
            try:
                return json.loads(raw)
            except Exception:
                return {"raw": raw}

    def _safe_text(self, value):
        text = str(value or "")
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _looks_garbled(self, text):
        t = str(text or "").strip()
        if not t:
            return False
        if "�" in t:
            return True
        return t.count("?") >= 2

    def _normalize_strategy_display_name(self, raw_name, rank_idx=None):
        name = str(raw_name or "").strip()
        dynamic_map = self._load_strategy_name_map()
        strategy_name_map = {
            "00": "长期持有一次买入",
            "01": "三周期共振波段",
            "02": "短线弱转强烂板",
            "03": "ETF行业轮动",
            "04": "龙头首阴反包",
            "05": "3N法则主升浪",
            "06": "海豚交易法",
            "07": "跳空交易系统",
            "08": "神奇九转",
            "09": "箱体降本策略"
        }
        m_any = re.match(r"^\s*([0-9A-Za-z]+)\s*-\s*(.*)$", name)
        sid_any = str(m_any.group(1) or "").strip() if m_any else ""
        mapped_name = str(dynamic_map.get(sid_any, "") or "").strip()
        if (not mapped_name) or self._looks_garbled(mapped_name):
            mapped_name = str(strategy_name_map.get(sid_any, "") or "").strip()
        if mapped_name:
            return f"{sid_any}-{mapped_name}"
        if (not name) or self._looks_garbled(name):
            if sid_any:
                return f"{sid_any}-策略"
            if rank_idx is not None:
                return f"策略{int(rank_idx)}"
            return "策略"
        return name

    def _load_strategy_name_map(self):
        now = time.time()
        if (now - float(self._strategy_name_map_at or 0.0)) < 30 and isinstance(self._strategy_name_map_cache, dict):
            return self._strategy_name_map_cache
        out = {}
        try:
            from src.strategies import strategy_factory as strategy_factory_module
            arr = strategy_factory_module.create_strategies(apply_active_filter=False)
            for s in arr:
                sid = str(getattr(s, "id", "") or "").strip()
                sname = str(getattr(s, "name", "") or "").strip()
                if sid and sname:
                    out[sid] = sname
        except Exception:
            out = {}
        self._strategy_name_map_cache = out
        self._strategy_name_map_at = now
        return out

    def _to_float(self, value):
        if value is None:
            return None
        if isinstance(value, str):
            t = value.strip().replace("%", "")
            if not t:
                return None
            try:
                return float(t)
            except Exception:
                return None
        try:
            return float(value)
        except Exception:
            return None

    def _to_percent_number(self, value):
        v = self._to_float(value)
        if v is None:
            return None
        if abs(v) <= 1.0:
            return v * 100.0
        return v

    def _fmt_amount(self, value):
        v = self._to_float(value)
        if v is None:
            return ""
        return f"{float(v):,.2f}"

    def _estimate_fee(self, direction, amount):
        amt = self._to_float(amount)
        if amt is None or amt <= 0:
            return None
        cfg = ConfigLoader.reload()
        commission_rate = float(cfg.get("trading_cost.commission_rate", 0.0) or 0.0)
        min_commission = float(cfg.get("trading_cost.min_commission", 0.0) or 0.0)
        stamp_duty_rate = float(cfg.get("trading_cost.stamp_duty", 0.0) or 0.0)
        transfer_fee_rate = float(cfg.get("trading_cost.transfer_fee", 0.0) or 0.0)
        commission = max(float(min_commission), float(amt) * float(commission_rate)) if commission_rate > 0 else float(min_commission)
        stamp_duty = float(amt) * float(stamp_duty_rate) if str(direction or "").upper() == "SELL" else 0.0
        transfer_fee = float(amt) * float(transfer_fee_rate) if transfer_fee_rate > 0 else 0.0
        total = float(commission) + float(stamp_duty) + float(transfer_fee)
        return {
            "total": total,
            "commission": commission,
            "stamp_duty": stamp_duty,
            "transfer_fee": transfer_fee
        }

    def _progress_bar(self, percent, width=12):
        p = self._to_percent_number(percent)
        if p is None:
            return "░" * int(width), None
        p = max(0.0, min(100.0, float(p)))
        filled = int(round((p / 100.0) * int(width)))
        return ("█" * filled) + ("░" * max(0, int(width) - filled)), p

    def _build_feishu_payload(self, event_type, stock_code, data):
        evt_label_map = {
            "trade_exec": ("成交信号", "green", "✅"),
            "live_alert": ("实盘告警", "red", "🚨"),
            "zhongshu": ("策略信号", "blue", "📈"),
            "menxia": ("风控结果", "orange", "🛡️"),
            "daily_summary": ("日终汇总", "indigo", "📘"),
            "system": ("系统消息", "wathet", "📣")
        }
        dir_color_map = {
            "BUY": ("买入", "green"),
            "SELL": ("卖出", "red")
        }
        level_label_map = {
            "ok": ("正常", "green"),
            "warn": ("预警", "orange"),
            "critical": ("严重", "red")
        }
        metric_label_map = {
            "daily_drawdown": "当日回撤",
            "single_position_weight": "单票仓位",
            "turnover_rate": "当日换手率",
            "signal_execution_delay_ms": "执行延迟",
            "expected_vs_actual_price_deviation": "成交偏差"
        }
        title, template, icon = evt_label_map.get(str(event_type or ""), (str(event_type or "事件通知"), "blue", "📨"))
        now_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if not isinstance(data, dict):
            text_body = (
                f"**标的**：`{self._safe_text(stock_code)}`\n"
                f"**时间**：{self._safe_text(now_text)}\n"
                f"**事件**：{self._safe_text(title)}\n"
                f"**内容**：{self._safe_text(data)}\n\n"
                f"<font color='red'>**⚠️ 免责声明：{self._safe_text(FEISHU_LIVE_DISCLAIMER)}**</font>"
            )
            return {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True, "enable_forward": True},
                    "header": {"template": template, "title": {"tag": "plain_text", "content": f"{icon} {title}"}},
                    "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": text_body}}]
                }
            }
        strategy_id = str(data.get("strategy_id", "") or "")
        strategy_name_raw = str(data.get("strategy_name", "") or "").strip()
        strategy_name = ""
        if strategy_name_raw and (not self._looks_garbled(strategy_name_raw)):
            strategy_name = self._normalize_strategy_display_name(strategy_name_raw)
        elif strategy_id:
            mapped_name = str(self._load_strategy_name_map().get(strategy_id, "") or "").strip()
            if mapped_name and (not self._looks_garbled(mapped_name)):
                strategy_name = f"{strategy_id}-{mapped_name}"
        direction = str(data.get("direction", "") or "").upper()
        direction_label, direction_color = dir_color_map.get(direction, ("", "grey"))
        if event_type == "trade_exec":
            if direction == "BUY":
                title = "买入-成交信号"
                template = "green"
            elif direction == "SELL":
                title = "卖出-成交信号"
                template = "red"
        if (not strategy_name) and strategy_id:
            strategy_name = self._normalize_strategy_display_name(strategy_id)
        qty_val = self._to_float(data.get("qty", None))
        hands_val = None if qty_val is None else (qty_val / 100.0)
        price_val = self._to_float(data.get("price", None))
        amount_val = self._to_float(data.get("amount", None))
        if amount_val is None and (price_val is not None) and (qty_val is not None):
            amount_val = float(price_val) * float(qty_val)
        fee_est = self._estimate_fee(direction, amount_val)
        level = str(data.get("level", "") or "").lower()
        level_label, level_color = level_label_map.get(level, ("", "grey"))
        metric = str(data.get("metric", "") or "")
        metric_label = metric_label_map.get(metric, metric)
        event_time = str(data.get("time", "") or "").strip()
        msg = str(data.get("msg", "") or "").strip()
        msg_garbled = self._looks_garbled(msg)
        if msg_garbled:
            if event_type == "zhongshu":
                msg = f"策略[{strategy_id or '--'}]生成信号：{direction_label or '--'} @ {data.get('price', '--')}"
            elif event_type == "menxia":
                decision = str(data.get("decision", "") or "").lower()
                msg = "风控审核通过" if decision == "approved" else ("风控审核拒绝" if decision == "rejected" else "风控状态更新")
            elif event_type == "trade_exec":
                msg = f"执行{direction_label or '--'}成交 @ {data.get('price', '--')}"
            elif event_type == "live_alert":
                msg = f"{metric_label or '指标'}触发{level_label or '告警'}"
            elif event_type == "daily_summary":
                msg = "日终汇总已生成"
            else:
                msg = "内容包含异常字符，已自动清洗展示"
        if event_type == "system":
            codes_raw = data.get("stock_codes", [])
            codes = []
            if isinstance(codes_raw, list):
                for item in codes_raw:
                    code = str(item or "").strip().upper()
                    if code and code not in codes:
                        codes.append(code)
            if not codes:
                code = str(stock_code or "").strip().upper()
                if code and code != "MULTI":
                    codes = [code]
            scope_text = "全局"
            if codes:
                scope_text = f"{len(codes)}只标的：{'、'.join(codes)}" if len(codes) > 1 else codes[0]
            lines = [
                f"**范围**：{self._safe_text(scope_text)}",
                f"**推送时间**：{self._safe_text(now_text)}"
            ]
            elements = [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}}
            ]
            if msg:
                elements.append({
                    "tag": "note",
                    "elements": [
                        {"tag": "lark_md", "content": f"**说明**：{self._safe_text(msg)}"}
                    ]
                })
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "lark_md", "content": f"<font color='red'>**⚠️ 免责声明：{self._safe_text(FEISHU_LIVE_DISCLAIMER)}**</font>"}
                ]
            })
            return {
                "msg_type": "interactive",
                "card": {
                    "config": {"wide_screen_mode": True, "enable_forward": True},
                    "header": {"template": template, "title": {"tag": "plain_text", "content": f"{icon} {title}"}},
                    "elements": elements
                }
            }
        kv_lines = [
            f"**标的**：`{self._safe_text(stock_code)}`",
            f"**推送时间**：{self._safe_text(now_text)}"
        ]
        if event_time:
            kv_lines.append(f"**信号时间**：{self._safe_text(event_time)}")
        if strategy_id:
            kv_lines.append(f"**策略ID**：`{self._safe_text(strategy_id)}`")
        if strategy_name:
            kv_lines.append(f"**策略名称**：`{self._safe_text(strategy_name)}`")
        if direction_label:
            kv_lines.append(f"**方向**：<font color='{direction_color}'>{self._safe_text(direction_label)}</font>")
        if data.get("price") is not None:
            kv_lines.append(f"**价格**：`{self._safe_text(data.get('price'))}`")
        if data.get("qty") is not None:
            kv_lines.append(f"**数量**：`{self._safe_text(data.get('qty'))}`")
        if hands_val is not None:
            kv_lines.append(f"**手数**：`{self._safe_text(f'{hands_val:.2f}手')}`")
        if amount_val is not None:
            amount_label = "买入金额" if direction == "BUY" else ("卖出金额" if direction == "SELL" else "成交金额")
            kv_lines.append(f"**{amount_label}**：`{self._safe_text(self._fmt_amount(amount_val))}`")
        if fee_est is not None:
            kv_lines.append(f"**预估手续费**：`{self._safe_text(self._fmt_amount(fee_est.get('total')))}`")
        if data.get("current_position_qty") is not None:
            kv_lines.append(f"**当前持仓数量**：`{self._safe_text(data.get('current_position_qty'))}`")
        if data.get("current_position_amount") is not None:
            kv_lines.append(f"**当前持仓金额**：`{self._safe_text(data.get('current_position_amount'))}`")
        if metric_label:
            kv_lines.append(f"**监控指标**：{self._safe_text(metric_label)}")
        if level_label:
            kv_lines.append(f"**告警级别**：<font color='{level_color}'>{self._safe_text(level_label)}</font>")
        if data.get("warn") is not None:
            kv_lines.append(f"**预警阈值**：`{self._safe_text(data.get('warn'))}`")
        if data.get("critical") is not None:
            kv_lines.append(f"**严重阈值**：`{self._safe_text(data.get('critical'))}`")
        content_main = "\n".join(kv_lines)
        elements = [{"tag": "div", "text": {"tag": "lark_md", "content": content_main}}]
        if event_type == "daily_summary":
            win_rate = self._to_percent_number(data.get("win_rate", None))
            max_dd = self._to_percent_number(data.get("max_drawdown", None))
            if max_dd is not None and abs(max_dd) <= 1.0:
                max_dd = max_dd * 100.0
            net_pnl = self._to_float(data.get("net_pnl", data.get("realized_pnl", None)))
            total_trades = int(data.get("total_trades", 0) or 0)
            win_rate_text = "--" if win_rate is None else f"{win_rate:.2f}%"
            max_dd_text = "--" if max_dd is None else f"{max_dd:.2f}%"
            net_pnl_text = "--" if net_pnl is None else f"{net_pnl:.2f}"
            win_color = "grey" if win_rate is None else ("red" if win_rate >= 55 else ("orange" if win_rate >= 40 else "green"))
            dd_color = "grey" if max_dd is None else ("green" if max_dd < 3 else ("orange" if max_dd < 6 else "red"))
            pnl_color = "grey" if net_pnl is None else ("red" if net_pnl >= 0 else "green")
            elements = [
                {"tag": "div", "text": {"tag": "lark_md", "content": (
                    "**📊 日终汇总看板**\n"
                    f"**汇总日期**：{self._safe_text(str(data.get('date', now_text[:10])))}\n"
                    f"**推送时间**：{self._safe_text(now_text)}\n"
                    f"**总交易数**：`{total_trades}`\n"
                    f"**胜率**：<font color='{win_color}'>{self._safe_text(win_rate_text)}</font>\n"
                    f"**当日净盈亏**：<font color='{pnl_color}'>{self._safe_text(net_pnl_text)}</font>\n"
                    f"**最大回撤**：<font color='{dd_color}'>{self._safe_text(max_dd_text)}</font>"
                )}}
            ]
            detail_rows = data.get("stock_summaries", [])
            detail_rows = detail_rows if isinstance(detail_rows, list) else []
            detail_lines = []
            rank_idx = 0
            for row in detail_rows:
                if not isinstance(row, dict):
                    continue
                rank_idx += 1
                row_code = str(row.get("stock_code", "") or "").strip().upper() or "--"
                row_trades = int(row.get("total_trades", 0) or 0)
                row_wr = self._to_percent_number(row.get("win_rate", None))
                row_dd = self._to_percent_number(row.get("max_drawdown", None))
                if row_dd is not None and abs(row_dd) <= 1.0:
                    row_dd = row_dd * 100.0
                row_pnl = self._to_float(row.get("net_pnl", row.get("realized_pnl", None)))
                row_wr_text = "--" if row_wr is None else f"{row_wr:.2f}%"
                row_dd_text = "--" if row_dd is None else f"{row_dd:.2f}%"
                row_pnl_text = "--" if row_pnl is None else f"{row_pnl:.2f}"
                detail_lines.append(
                    f"{rank_idx}. `{self._safe_text(row_code)}`｜交易:`{row_trades}`｜胜率:`{self._safe_text(row_wr_text)}`｜净盈亏:`{self._safe_text(row_pnl_text)}`｜回撤:`{self._safe_text(row_dd_text)}`"
                )
            if detail_lines:
                elements.append({
                    "tag": "div",
                    "text": {
                        "tag": "lark_md",
                        "content": "**📋 按标的明细**\n" + "\n".join(detail_lines)
                    }
                })
        if msg:
            elements.append({
                "tag": "note",
                "elements": [
                    {"tag": "lark_md", "content": f"**说明**：{self._safe_text(msg)}"}
                ]
            })
        elements.append({
            "tag": "note",
            "elements": [
                {"tag": "lark_md", "content": f"<font color='red'>**⚠️ 免责声明：{self._safe_text(FEISHU_LIVE_DISCLAIMER)}**</font>"}
            ]
        })
        return {
            "msg_type": "interactive",
            "card": {
                "config": {"wide_screen_mode": True, "enable_forward": True},
                "header": {"template": template, "title": {"tag": "plain_text", "content": f"{icon} {title}"}},
                "elements": elements
            }
        }

    def _failed_file(self, cfg):
        path = str(cfg.get("failed_events_file", "data/webhook_failed_events.jsonl") or "data/webhook_failed_events.jsonl").strip()
        if os.path.isabs(path):
            return path
        base_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(os.path.dirname(base_dir))
        return os.path.join(project_root, path)

    def _append_failed(self, cfg, record):
        if not bool(cfg.get("persist_failed_events", True)):
            return
        file_path = self._failed_file(cfg)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        line = json.dumps(record, ensure_ascii=False) + "\n"
        with self._failed_lock:
            with open(file_path, "a", encoding="utf-8") as f:
                f.write(line)

    def _load_failed(self, cfg):
        file_path = self._failed_file(cfg)
        if not os.path.exists(file_path):
            return []
        rows = []
        with self._failed_lock:
            with open(file_path, "r", encoding="utf-8") as f:
                for line in f:
                    text = str(line or "").strip()
                    if not text:
                        continue
                    try:
                        item = json.loads(text)
                        if isinstance(item, dict):
                            rows.append(item)
                    except Exception:
                        continue
        return rows

    def _rewrite_failed(self, cfg, rows):
        file_path = self._failed_file(cfg)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with self._failed_lock:
            with open(file_path, "w", encoding="utf-8") as f:
                for row in rows:
                    f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _row_id(self, row, idx=0):
        raw = json.dumps({
            "channel_type": row.get("channel_type"),
            "url": row.get("url"),
            "event_type": row.get("event_type"),
            "stock_code": row.get("stock_code"),
            "created_at": row.get("created_at"),
            "idx": idx
        }, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def get_failed_events(self, limit=200):
        cfg = self._load_cfg()
        rows = self._load_failed(cfg)
        out = []
        cap = max(1, int(limit or 200))
        for idx, row in enumerate(rows[-cap:]):
            item = dict(row)
            item["event_id"] = self._row_id(item, idx)
            out.append(item)
        return out

    def delete_failed_events(self, event_ids=None):
        cfg = self._load_cfg()
        rows = self._load_failed(cfg)
        if not rows:
            return {"deleted_count": 0, "remaining_count": 0}
        if isinstance(event_ids, list) and event_ids:
            target = set(str(x or "").strip() for x in event_ids if str(x or "").strip())
            remain = []
            deleted = 0
            for idx, row in enumerate(rows):
                rid = self._row_id(row, idx)
                if rid in target:
                    deleted += 1
                else:
                    remain.append(row)
            self._rewrite_failed(cfg, remain)
            return {"deleted_count": deleted, "remaining_count": len(remain)}
        self._rewrite_failed(cfg, [])
        return {"deleted_count": len(rows), "remaining_count": 0}

    async def retry_failed_events(self, event_ids=None, limit=20):
        cfg = self._load_cfg()
        rows = self._load_failed(cfg)
        if not rows:
            return {"retried": 0, "success": 0, "failed": 0, "remaining_count": 0}
        id_set = None
        if isinstance(event_ids, list) and event_ids:
            id_set = set(str(x or "").strip() for x in event_ids if str(x or "").strip())
            if not id_set:
                id_set = None
        cap = max(1, int(limit or 20))
        timeout_sec = float(cfg.get("timeout_sec", 5) or 5)
        feishu_secret = str(cfg.get("feishu_secret", "") or "").strip()
        retry_backoff_seconds = float(cfg.get("retry_backoff_seconds", 1.2) or 1.2)
        max_retry = max(1, int(cfg.get("failed_max_retry", 20) or 20))
        retried = 0
        success = 0
        failed = 0
        remain = []
        for idx, row in enumerate(rows):
            rid = self._row_id(row, idx)
            if retried >= cap:
                remain.append(row)
                continue
            if id_set is not None and rid not in id_set:
                remain.append(row)
                continue
            channel_type = str(row.get("channel_type", "") or "")
            url = str(row.get("url", "") or "")
            event_type = str(row.get("event_type", "") or "")
            stock_code = str(row.get("stock_code", "") or "")
            data = row.get("data")
            if (not channel_type) or (not url) or (not event_type) or (not stock_code):
                failed += 1
                retried += 1
                continue
            retried += 1
            try:
                await self._send_job(channel_type, url, event_type, data, stock_code, timeout_sec, feishu_secret)
                success += 1
            except Exception as e:
                failed += 1
                retries = int(row.get("retries", 0) or 0) + 1
                if retries > max_retry:
                    logger.error("webhook manual retry dropped channel=%s url=%s err=%s", channel_type, url, e)
                    continue
                row["retries"] = retries
                row["last_error"] = str(e)
                row["next_retry_at"] = time.time() + max(1.0, retry_backoff_seconds)
                remain.append(row)
        self._rewrite_failed(cfg, remain)
        return {
            "retried": retried,
            "success": success,
            "failed": failed,
            "remaining_count": len(remain)
        }

    async def _send_job(self, channel_type, url, event_type, data, stock_code, timeout_sec, feishu_secret):
        text = self._build_text(event_type, stock_code, data)
        if channel_type == "feishu":
            if not self._allow_feishu_event(event_type, data):
                return
            payload = self._build_feishu_payload(event_type, stock_code, data)
            if feishu_secret:
                ts, sign = self._feishu_sign(feishu_secret)
                payload["timestamp"] = ts
                payload["sign"] = sign
        else:
            generic_data = data
            if isinstance(data, dict):
                generic_data = dict(data)
                generic_data["disclaimer"] = FEISHU_LIVE_DISCLAIMER
            else:
                generic_data = {
                    "content": data,
                    "disclaimer": FEISHU_LIVE_DISCLAIMER
                }
            payload = {
                "event_type": event_type,
                "stock_code": stock_code,
                "data": generic_data,
                "timestamp": datetime.now().isoformat(timespec="seconds")
            }
        await asyncio.to_thread(self._post_json, url, payload, timeout_sec)

    async def _retry_failed_once(self, cfg):
        if not bool(cfg.get("persist_failed_events", True)):
            return
        rows = self._load_failed(cfg)
        if not rows:
            return
        now_ts = time.time()
        max_retry = max(1, int(cfg.get("failed_max_retry", 20) or 20))
        retry_batch_size = max(1, int(cfg.get("failed_retry_batch_size", 20) or 20))
        timeout_sec = float(cfg.get("timeout_sec", 5) or 5)
        retry_backoff_seconds = float(cfg.get("retry_backoff_seconds", 1.2) or 1.2)
        feishu_secret = str(cfg.get("feishu_secret", "") or "").strip()
        processed = 0
        keep_rows = []
        for row in rows:
            due_ts = float(row.get("next_retry_at", 0) or 0)
            if due_ts > now_ts:
                keep_rows.append(row)
                continue
            if processed >= retry_batch_size:
                keep_rows.append(row)
                continue
            channel_type = str(row.get("channel_type", "") or "")
            url = str(row.get("url", "") or "")
            event_type = str(row.get("event_type", "") or "")
            stock_code = str(row.get("stock_code", "") or "")
            data = row.get("data")
            if (not channel_type) or (not url) or (not event_type) or (not stock_code):
                continue
            try:
                await self._send_job(channel_type, url, event_type, data, stock_code, timeout_sec, feishu_secret)
                processed += 1
            except Exception as e:
                processed += 1
                retries = int(row.get("retries", 0) or 0) + 1
                if retries > max_retry:
                    logger.error("webhook failed event dropped channel=%s url=%s err=%s", channel_type, url, e)
                    continue
                row["retries"] = retries
                row["last_error"] = str(e)
                row["next_retry_at"] = time.time() + min(600.0, retry_backoff_seconds * (2 ** min(retries, 8)))
                keep_rows.append(row)
        self._rewrite_failed(cfg, keep_rows)

    async def _maybe_retry_failed(self, cfg):
        interval = max(1.0, float(cfg.get("failed_retry_interval_seconds", 30) or 30))
        now_ts = time.time()
        if (now_ts - self._last_retry_ts) < interval:
            return
        async with self._retry_lock:
            now_ts = time.time()
            if (now_ts - self._last_retry_ts) < interval:
                return
            self._last_retry_ts = now_ts
            await self._retry_failed_once(cfg)

    async def notify(self, event_type, data, stock_code, force=False):
        cfg = self._load_cfg()
        if not bool(cfg.get("enabled", False)):
            return
        await self._maybe_retry_failed(cfg)
        event_types = cfg.get("event_types", ["trade_exec", "live_alert", "zhongshu", "menxia", "daily_summary", "system"])
        event_types = event_types if isinstance(event_types, list) else []
        if event_types and (event_type not in event_types):
            return
        generic_urls = cfg.get("webhook_urls", [])
        generic_urls = generic_urls if isinstance(generic_urls, list) else []
        generic_urls = [str(u or "").strip() for u in generic_urls if str(u or "").strip()]
        feishu_url = str(cfg.get("feishu_webhook_url", "") or "").strip()
        feishu_secret = str(cfg.get("feishu_secret", "") or "").strip()
        jobs = []
        for url in generic_urls:
            jobs.append(("generic", url))
        if feishu_url and self._allow_feishu_event(event_type, data):
            jobs.append(("feishu", feishu_url))
        if not jobs:
            return
        dedupe_window_seconds = float(cfg.get("dedupe_window_seconds", 12) or 12)
        if (not bool(force)) and (not self._should_send(event_type, stock_code, data, dedupe_window_seconds)):
            return
        timeout_sec = float(cfg.get("timeout_sec", 5) or 5)
        max_retries = max(0, int(cfg.get("max_retries", 2) or 2))
        retry_backoff_seconds = float(cfg.get("retry_backoff_seconds", 1.2) or 1.2)
        for channel_type, url in jobs:
            last_error = ""
            for attempt in range(max_retries + 1):
                try:
                    await self._send_job(channel_type, url, event_type, data, stock_code, timeout_sec, feishu_secret)
                    last_error = ""
                    break
                except urllib.error.HTTPError as e:
                    last_error = f"http_error:{e.code}"
                    if attempt >= max_retries:
                        logger.error("webhook notify http error url=%s code=%s", url, e.code)
                        break
                    await asyncio.sleep(retry_backoff_seconds * (attempt + 1))
                except Exception as e:
                    last_error = str(e)
                    if attempt >= max_retries:
                        logger.error("webhook notify failed url=%s err=%s", url, e)
                        break
                    await asyncio.sleep(retry_backoff_seconds * (attempt + 1))
            if last_error:
                self._append_failed(cfg, {
                    "channel_type": channel_type,
                    "url": url,
                    "event_type": event_type,
                    "stock_code": stock_code,
                    "data": data,
                    "created_at": datetime.now().isoformat(timespec="seconds"),
                    "retries": 0,
                    "next_retry_at": time.time() + max(1.0, retry_backoff_seconds),
                    "last_error": last_error
                })
