import json
import inspect
import os
import re
from datetime import datetime
from src.strategies.implemented_strategies import (
    Strategy00, Strategy01, Strategy02, Strategy03, Strategy04, Strategy05,
    Strategy06, Strategy07, Strategy08, Strategy09, BaseImplementedStrategy
)
from src.utils.indicators import Indicators
import pandas as pd
import numpy as np
from src.utils.runtime_params import get_value
from src.strategy_intent.intent_engine import StrategyIntentEngine

_BUILTIN_STRATEGY_CLASSES = {
    "00": Strategy00,
    "01": Strategy01,
    "02": Strategy02,
    "03": Strategy03,
    "04": Strategy04,
    "05": Strategy05,
    "06": Strategy06,
    "07": Strategy07,
    "08": Strategy08,
    "09": Strategy09,
}
_BUILTIN_META_CACHE = None
_BUILTIN_USAGE_NOTICE = "当前为内置策略，仅供测试研究使用，非投资建议，不构成买卖依据。"


def _project_root():
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _data_dir():
    return os.path.join(_project_root(), "data", "strategies")


def custom_store_path():
    return os.path.join(_data_dir(), "custom_strategies.json")


def custom_private_store_path():
    override = str(os.environ.get("CUSTOM_STRATEGIES_PRIVATE_PATH", "") or "").strip()
    if override:
        return override
    cfg = {}
    cfg_path = os.path.join(_project_root(), "config.json")
    if os.path.exists(cfg_path):
        try:
            with open(cfg_path, "r", encoding="utf-8") as f:
                cfg = json.load(f) or {}
        except Exception:
            cfg = {}
    system_cfg = cfg.get("system", {}) if isinstance(cfg, dict) else {}
    cfg_override = str(system_cfg.get("private_strategy_path", "") or "").strip()
    if cfg_override:
        return cfg_override if os.path.isabs(cfg_override) else os.path.join(_project_root(), cfg_override)
    return os.path.join(_data_dir(), "custom_strategies.private.json")


def _resolve_custom_store_path(for_write=False):
    private_path = custom_private_store_path()
    if os.path.exists(private_path):
        return private_path
    if for_write and str(os.environ.get("CUSTOM_STRATEGIES_WRITE_PRIVATE", "")).strip() == "1":
        return private_path
    return custom_store_path()


def state_store_path():
    return os.path.join(_data_dir(), "strategy_state.json")


def ensure_strategy_store():
    os.makedirs(_data_dir(), exist_ok=True)
    if not os.path.exists(custom_store_path()):
        with open(custom_store_path(), "w", encoding="utf-8") as f:
            json.dump({"strategies": []}, f, ensure_ascii=False, indent=2)
    if not os.path.exists(state_store_path()):
        with open(state_store_path(), "w", encoding="utf-8") as f:
            json.dump({"disabled_ids": [], "deleted_ids": []}, f, ensure_ascii=False, indent=2)


def _load_state_payload():
    ensure_strategy_store()
    try:
        with open(state_store_path(), "r", encoding="utf-8") as f:
            payload = json.load(f)
        if not isinstance(payload, dict):
            payload = {}
    except Exception:
        payload = {}
    if not isinstance(payload.get("disabled_ids"), list):
        payload["disabled_ids"] = []
    if not isinstance(payload.get("deleted_ids"), list):
        payload["deleted_ids"] = []
    return payload


def _save_state_payload(payload):
    ensure_strategy_store()
    data = payload if isinstance(payload, dict) else {}
    if not isinstance(data.get("disabled_ids"), list):
        data["disabled_ids"] = []
    if not isinstance(data.get("deleted_ids"), list):
        data["deleted_ids"] = []
    with open(state_store_path(), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _build_builtin_strategy_code(strategy_id):
    sid = str(strategy_id or "").strip()
    cls = _BUILTIN_STRATEGY_CLASSES.get(sid)
    if cls is None:
        return ""
    try:
        class_src = inspect.getsource(cls).strip()
    except Exception:
        return ""
    header = (
        "from src.strategies.implemented_strategies import BaseImplementedStrategy\n"
        "import pandas as pd\n"
        "import numpy as np\n"
        "from src.utils.indicators import Indicators\n"
        "from src.utils.runtime_params import get_value\n\n"
    )
    return f"{header}{class_src}\n"


def _build_builtin_strategy_analysis(meta, code_text):
    name = str((meta or {}).get("name", "")).strip() or str((meta or {}).get("id", "")).strip()
    kline_type = str((meta or {}).get("kline_type", "1min")).strip() or "1min"
    code = str(code_text or "").upper()
    features = []
    if "MACD" in code:
        features.append("MACD动量确认")
    if "EMA" in code:
        features.append("EMA趋势过滤")
    if "MA(" in code or "INDICATORS.MA" in code:
        features.append("均线结构判断")
    if "RSI" in code:
        features.append("RSI超买超卖")
    if "BOLL" in code:
        features.append("布林带波动约束")
    if "STOP_LOSS" in code or "TRAILING_STOP" in code:
        features.append("止损风控")
    if "TAKE_PROFIT" in code or "TP1" in code:
        features.append("止盈管理")
    if "CHECK_MAX_HOLDING_TIME" in code:
        features.append("持仓时长约束")
    if not features:
        features.append("规则驱动交易")
    return f"内置策略「{name}」，触发周期={kline_type}，核心特征：{'、'.join(features)}。详情以下方代码实现为准。{_BUILTIN_USAGE_NOTICE}"


def list_builtin_strategy_meta():
    global _BUILTIN_META_CACHE
    if isinstance(_BUILTIN_META_CACHE, list) and _BUILTIN_META_CACHE:
        return [dict(x) for x in _BUILTIN_META_CACHE]
    items = [
        Strategy00(), Strategy01(), Strategy02(), Strategy03(), Strategy04(),
        Strategy05(), Strategy06(), Strategy07(), Strategy08(), Strategy09()
    ]
    out = []
    for s in items:
        sid = str(s.id)
        item = {
            "id": sid,
            "name": str(s.name),
            "builtin": True,
            "kline_type": str(getattr(s, "trigger_timeframe", "1min") or "1min")
        }
        code_text = _build_builtin_strategy_code(sid)
        item["code"] = code_text
        item["analysis_text"] = _build_builtin_strategy_analysis(item, code_text)
        item["raw_requirement_title"] = "内置策略说明"
        item["raw_requirement"] = f"内置策略 {item['name']}（ID={sid}）由系统内置维护，可查看代码但不可直接编辑。{_BUILTIN_USAGE_NOTICE}"
        item["usage_notice"] = _BUILTIN_USAGE_NOTICE
        out.append(item)
    _BUILTIN_META_CACHE = [dict(x) for x in out]
    return out


def infer_kline_type_from_code(code_text):
    code = str(code_text or "")
    m = re.search(r"trigger_timeframe\s*=\s*['\"]([^'\"]+)['\"]", code)
    if m:
        return str(m.group(1)).strip() or "1min"
    return "1min"


def normalize_kline_type(value):
    txt = str(value or "").strip()
    return txt or "1min"


def _normalize_depends_on(value):
    if not isinstance(value, list):
        return []
    out = []
    seen = set()
    for item in value:
        sid = str(item or "").strip()
        if not sid:
            continue
        if sid in seen:
            continue
        seen.add(sid)
        out.append(sid)
    return out


def _is_garbled_text(value):
    txt = str(value or "")
    return ("�" in txt) or ("??" in txt)


def _infer_strategy_name(sid, class_name, current_name):
    curr = str(current_name or "").strip()
    if curr and (not _is_garbled_text(curr)):
        return curr
    sid_txt = str(sid or "").strip()
    cls = str(class_name or "").strip()
    if sid_txt == "34":
        return "OliverKellEMA交易法则"
    if sid_txt.startswith("34A") and sid_txt != "34A":
        return f"OliverKellEMA交易法则-改进{sid_txt}"
    if sid_txt == "34A":
        return "OliverKellEMA交易法则-改进A"
    if sid_txt == "34R1":
        return "OliverKellEMA策略路由R1"
    if sid_txt.startswith("34R1_"):
        return f"OliverKellEMA多周期路由-{sid_txt}"
    if sid_txt.startswith("34R1U500"):
        return f"OliverKellEMA冲顶族-{sid_txt}"
    if sid_txt.startswith("34R1U"):
        return f"OliverKellEMA利用率增强-{sid_txt}"
    if cls.startswith("OliverKellEMA"):
        return f"OliverKellEMA-{sid_txt}" if sid_txt else "OliverKellEMA策略"
    return curr or sid_txt


def _infer_intent_indicators(code_text, old_indicators):
    old = old_indicators if isinstance(old_indicators, list) else []
    if old:
        return [str(x).strip() for x in old if str(x).strip()]
    code = str(code_text or "").upper()
    indicators = []
    for key in ["EMA", "MA", "MACD", "RSI", "ATR", "BOLL", "VOLUME"]:
        if key in code:
            indicators.append(key)
    return indicators


def _normalize_super_init_title(code_text, strategy_name):
    code = str(code_text or "")
    if not code.strip():
        return code
    pattern = r"(super\(\)\.__init__\(\s*['\"][^'\"]+['\"]\s*,\s*)(['\"])([^'\"]*)(['\"])"
    m = re.search(pattern, code)
    if not m:
        return code
    old_name = str(m.group(3) or "")
    if not _is_garbled_text(old_name):
        return code
    new_name = str(strategy_name or "").replace("'", " ").replace('"', " ").strip() or old_name
    return re.sub(pattern, rf"\1\2{new_name}\4", code, count=1)


def _repair_garbled_rows(rows):
    if not isinstance(rows, list):
        return rows, False
    changed = False
    out = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        r = dict(row)
        sid = str(r.get("id", "")).strip()
        code = str(r.get("code", ""))
        inferred_name = _infer_strategy_name(sid, r.get("class_name", ""), r.get("name", ""))
        if inferred_name and str(r.get("name", "")) != inferred_name:
            r["name"] = inferred_name
            changed = True
        repaired_code = _normalize_super_init_title(code, inferred_name)
        if repaired_code != code:
            r["code"] = repaired_code
            code = repaired_code
            changed = True
        trigger_tf = str(r.get("kline_type", "")).strip() or infer_kline_type_from_code(code)
        feature_parts = []
        if "EMA" in code.upper():
            feature_parts.append("EMA趋势")
        if "tp1_done" in code:
            feature_parts.append("分批止盈")
        if "pending_sell" in code:
            feature_parts.append("跌停延迟卖出")
        if "last_buy_day" in code:
            feature_parts.append("T+1约束")
        if not feature_parts:
            feature_parts.append("趋势跟随")
        logic_text = f"{sid}：{'+'.join(feature_parts)}，周期={trigger_tf}，包含A股风控约束。"
        analysis_text = f"{logic_text} 重点执行趋势确认入场、风控优先退出与仓位管理。"
        raw_text = f"基于代码行为推断：{logic_text} 入场偏向突破/趋势确认，退出包含止损、止盈与时间窗约束。"
        if _is_garbled_text(r.get("analysis_text", "")):
            r["analysis_text"] = analysis_text
            changed = True
        if _is_garbled_text(r.get("raw_requirement_title", "")):
            r["raw_requirement_title"] = "策略模板"
            changed = True
        if _is_garbled_text(r.get("raw_requirement", "")):
            r["raw_requirement"] = raw_text
            changed = True
        intent = r.get("strategy_intent") if isinstance(r.get("strategy_intent"), dict) else {}
        if not isinstance(intent, dict):
            intent = {}
        if str(intent.get("source", "")).strip().lower() not in {"human", "market"}:
            intent["source"] = "human"
            changed = True
        if not str(intent.get("strategy_type", "")).strip():
            intent["strategy_type"] = "trend_following"
            changed = True
        if _is_garbled_text(intent.get("logic", "")):
            intent["logic"] = logic_text
            changed = True
        indicators = _infer_intent_indicators(code, intent.get("indicators"))
        if indicators != (intent.get("indicators") if isinstance(intent.get("indicators"), list) else []):
            intent["indicators"] = indicators
            changed = True
        if not str(intent.get("entry", "")).strip():
            intent["entry"] = "满足趋势与风控条件时开仓"
            changed = True
        if not str(intent.get("exit", "")).strip():
            intent["exit"] = "反向信号或风控触发时平仓"
            changed = True
        if not str(intent.get("risk_profile", "")).strip():
            intent["risk_profile"] = "balanced"
            changed = True
        if not isinstance(intent.get("confidence"), (int, float)):
            intent["confidence"] = 0.72
            changed = True
        r["strategy_intent"] = intent
        explain = str(r.get("intent_explain", "")).strip()
        if (not explain) or _is_garbled_text(explain):
            ind_text = "、".join([str(x).strip() for x in intent.get("indicators", []) if str(x).strip()]) or "无"
            r["intent_explain"] = (
                f"来源={intent.get('source', 'human')}; 类型={intent.get('strategy_type', 'trend_following')}; "
                f"逻辑={intent.get('logic', logic_text)}; 指标={ind_text}; 入场={intent.get('entry', '')}; "
                f"出场={intent.get('exit', '')}; 风格={intent.get('risk_profile', 'balanced')}; "
                f"置信度={float(intent.get('confidence', 0.72)):.2f}"
            )
            changed = True
        out.append(r)
    return out, changed


def is_builtin_strategy_id(strategy_id):
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    builtin_ids = {str(x.get("id", "")).strip() for x in list_builtin_strategy_meta()}
    return sid in builtin_ids


def list_strategy_dependents(strategy_id):
    target = str(strategy_id or "").strip()
    if not target:
        return []
    out = []
    for row in load_custom_strategies():
        sid = str(row.get("id", "")).strip()
        if not sid:
            continue
        deps = _normalize_depends_on(row.get("depends_on"))
        if target in deps:
            out.append(sid)
    return sorted(out)


def load_custom_strategies():
    ensure_strategy_store()
    store_path = _resolve_custom_store_path(for_write=False)
    encodings = ["utf-8-sig", "utf-8", "gbk", "cp936"]
    for enc in encodings:
        try:
            with open(store_path, "r", encoding=enc) as f:
                payload = json.load(f)
            rows = payload.get("strategies", []) if isinstance(payload, dict) else []
            safe_rows = [r for r in rows if isinstance(r, dict)]
            fixed_rows, changed = _repair_garbled_rows(safe_rows)
            if changed:
                save_custom_strategies(fixed_rows)
            return fixed_rows
        except Exception:
            continue
    return []


def save_custom_strategies(rows):
    ensure_strategy_store()
    safe_rows = [r for r in rows if isinstance(r, dict)]
    store_path = _resolve_custom_store_path(for_write=True)
    folder = os.path.dirname(store_path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    with open(store_path, "w", encoding="utf-8") as f:
        json.dump({"strategies": safe_rows}, f, ensure_ascii=False, indent=2)


def load_disabled_ids():
    try:
        payload = _load_state_payload()
        rows = payload.get("disabled_ids", [])
        return set(str(x) for x in rows if str(x).strip())
    except Exception:
        return set()


def save_disabled_ids(ids):
    unique_ids = sorted(set(str(x) for x in ids if str(x).strip()))
    payload = _load_state_payload()
    payload["disabled_ids"] = unique_ids
    _save_state_payload(payload)


def load_deleted_ids():
    try:
        payload = _load_state_payload()
        rows = payload.get("deleted_ids", [])
        return set(str(x) for x in rows if str(x).strip())
    except Exception:
        return set()


def save_deleted_ids(ids):
    unique_ids = sorted(set(str(x) for x in ids if str(x).strip()))
    payload = _load_state_payload()
    payload["deleted_ids"] = unique_ids
    _save_state_payload(payload)


def list_all_strategy_meta():
    builtin = list_builtin_strategy_meta()
    custom = load_custom_strategies()
    disabled = load_disabled_ids()
    deleted = load_deleted_ids()
    builtin_ids = {str(b["id"]).strip() for b in builtin}
    out = []
    for b in builtin:
        sid = str(b["id"])
        if sid in deleted:
            continue
        out.append({
            "id": sid,
            "name": str(b["name"]),
            "builtin": True,
            "kline_type": str(b.get("kline_type", "1min")),
            "enabled": sid not in disabled,
            "deletable": True,
            "editable": False,
            "source": "builtin",
            "source_label": "内置策略",
            "protect_level": "builtin",
            "immutable": True,
            "depends_on": [],
            "analysis_text": str(b.get("analysis_text", "")),
            "code": str(b.get("code", "")),
            "raw_requirement_title": str(b.get("raw_requirement_title", "内置策略说明")),
            "raw_requirement": str(b.get("raw_requirement", "")),
            "usage_notice": str(b.get("usage_notice", _BUILTIN_USAGE_NOTICE))
        })
    for c in custom:
        sid = str(c.get("id", "")).strip()
        if not sid:
            continue
        if sid in builtin_ids:
            continue
        if sid in deleted:
            continue
        source = str(c.get("source", "")).strip().lower()
        if source not in {"human", "market"}:
            source = str(((c.get("strategy_intent") or {}).get("source", ""))).strip().lower()
        if source not in {"human", "market"}:
            source = "human"
        source_label = "用户输入" if source == "human" else "行情驱动"
        raw_requirement_title = str(c.get("raw_requirement_title", "")).strip()
        if not raw_requirement_title:
            raw_requirement_title = "策略模板" if source == "human" else "行情状态"
        raw_requirement = str(c.get("raw_requirement", "")).strip()
        if not raw_requirement:
            raw_requirement = str(c.get("template_text", "")).strip()
        out.append({
            "id": sid,
            "name": str(c.get("name", sid)),
            "builtin": False,
            "kline_type": str(c.get("kline_type", "")).strip() or infer_kline_type_from_code(c.get("code", "")),
            "enabled": sid not in disabled,
            "deletable": True,
            "editable": True,
            "source": source,
            "source_label": source_label,
            "protect_level": str(c.get("protect_level", "custom")).strip() or "custom",
            "immutable": bool(c.get("immutable", False)),
            "depends_on": _normalize_depends_on(c.get("depends_on")),
            "analysis_text": str(c.get("analysis_text", "")),
            "code": str(c.get("code", "")),
            "raw_requirement_title": raw_requirement_title,
            "raw_requirement": raw_requirement,
            "usage_notice": str(c.get("usage_notice", ""))
        })
    out.sort(key=lambda x: x["id"])
    return out


def next_custom_strategy_id():
    used_numeric = set()
    deleted = load_deleted_ids()
    for b in list_builtin_strategy_meta():
        sid = str(b["id"]).strip()
        if sid in deleted:
            continue
        if sid.isdigit():
            used_numeric.add(int(sid))
    for c in load_custom_strategies():
        sid = str(c.get("id", "")).strip()
        if sid in deleted:
            continue
        if sid.isdigit():
            used_numeric.add(int(sid))
    i = 0
    while True:
        if i not in used_numeric:
            sid = f"{i:02d}" if i < 100 else str(i)
            return sid
        i += 1


def _sanitize_class_name(raw):
    txt = re.sub(r"[^0-9a-zA-Z_]", "", str(raw or ""))
    if not txt:
        txt = "GeneratedStrategy"
    if txt[0].isdigit():
        txt = f"S{txt}"
    return txt


def normalize_strategy_intent(payload):
    engine = StrategyIntentEngine()
    intent = engine.normalize(payload)
    return intent.to_dict(), intent.explain()


def build_fallback_strategy_code(strategy_id, strategy_name, template_text):
    cls = _sanitize_class_name(f"GeneratedStrategy{strategy_id}")
    title = str(strategy_name or f"AI策略{strategy_id}")
    return f"""from src.strategies.implemented_strategies import BaseImplementedStrategy
import pandas as pd
from src.utils.indicators import Indicators

class {cls}(BaseImplementedStrategy):
    def __init__(self):
        super().__init__(\"{strategy_id}\", \"{title}\", trigger_timeframe=\"1min\")
        self.history = {{}}
        self.last_buy_day = {{}}

    def on_bar(self, kline):
        code = kline['code']
        if code not in self.history:
            self.history[code] = pd.DataFrame()
        self.history[code] = pd.concat([self.history[code], pd.DataFrame([kline])], ignore_index=True).tail(2000)
        df = self.history[code]
        if len(df) < 80:
            return None
        close = df['close']
        ma_fast = Indicators.MA(close, 12)
        ma_slow = Indicators.MA(close, 36)
        if len(ma_fast) < 2 or len(ma_slow) < 2:
            return None
        qty = int(self.positions.get(code, 0))
        c = float(kline['close'])
        stop_loss_pct = float(self._cfg(\"stop_loss_pct\", 0.03))
        if qty <= 0 and float(ma_fast.iloc[-2]) <= float(ma_slow.iloc[-2]) and float(ma_fast.iloc[-1]) > float(ma_slow.iloc[-1]):
            buy_qty = int(self._qty())
            if buy_qty <= 0:
                return None
            self.last_buy_day[code] = str(pd.to_datetime(kline['dt'], errors='coerce').strftime('%Y-%m-%d'))
            return {{
                'strategy_id': self.id,
                'code': code,
                'dt': kline['dt'],
                'direction': 'BUY',
                'price': c,
                'qty': buy_qty,
                'stop_loss': c * (1 - stop_loss_pct),
                'take_profit': None
            }}
        if qty > 0 and float(ma_fast.iloc[-2]) >= float(ma_slow.iloc[-2]) and float(ma_fast.iloc[-1]) < float(ma_slow.iloc[-1]):
            curr_day = str(pd.to_datetime(kline['dt'], errors='coerce').strftime('%Y-%m-%d'))
            if self.last_buy_day.get(code) == curr_day:
                return None
            return self.create_exit_signal(kline, qty, \"MA Cross Exit\")
        return None
"""


def add_custom_strategy(entry):
    rows = load_custom_strategies()
    sid = str(entry.get("id", "")).strip()
    if not sid:
        raise ValueError("strategy id is required")
    builtin_ids = {str(x.get("id", "")).strip() for x in list_builtin_strategy_meta()}
    if sid in builtin_ids:
        raise ValueError(f"strategy id conflicts with builtin strategy: {sid}")
    if any(str(r.get("id", "")).strip() == sid for r in rows):
        raise ValueError(f"strategy id already exists: {sid}")
    intent_payload = entry.get("strategy_intent")
    if not isinstance(intent_payload, dict):
        raise ValueError("strategy_intent is required")
    strategy_intent, intent_explain = normalize_strategy_intent(intent_payload)
    source = str(entry.get("source", "")).strip().lower()
    if source not in {"human", "market"}:
        source = str(strategy_intent.get("source", "")).strip().lower()
    if source not in {"human", "market"}:
        source = "human"
    raw_requirement_title = str(entry.get("raw_requirement_title", "")).strip()
    if not raw_requirement_title:
        raw_requirement_title = "策略模板" if source == "human" else "行情状态"
    raw_requirement = str(entry.get("raw_requirement", "")).strip()
    if not raw_requirement:
        raw_requirement = str(entry.get("template_text", "")).strip()
    kline_type = normalize_kline_type(entry.get("kline_type"))
    now = datetime.now().isoformat(timespec="seconds")
    row = {
        "id": sid,
        "name": str(entry.get("name", sid)),
        "class_name": str(entry.get("class_name", "")),
        "code": str(entry.get("code", "")),
        "kline_type": kline_type,
        "template_text": str(entry.get("template_text", "")),
        "analysis_text": str(entry.get("analysis_text", "")),
        "source": source,
        "protect_level": str(entry.get("protect_level", "custom") or "custom").strip() or "custom",
        "immutable": bool(entry.get("immutable", False)),
        "depends_on": _normalize_depends_on(entry.get("depends_on")),
        "raw_requirement_title": raw_requirement_title,
        "raw_requirement": raw_requirement,
        "strategy_intent": strategy_intent,
        "intent_explain": intent_explain,
        "created_at": now,
        "updated_at": now
    }
    rows.append(row)
    save_custom_strategies(rows)


def delete_custom_strategy(strategy_id):
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    rows = load_custom_strategies()
    new_rows = [r for r in rows if str(r.get("id", "")).strip() != sid]
    changed = len(new_rows) != len(rows)
    if changed:
        save_custom_strategies(new_rows)
    disabled = load_disabled_ids()
    if sid in disabled:
        disabled.remove(sid)
        save_disabled_ids(disabled)
    return changed


def delete_strategy(strategy_id):
    sid = str(strategy_id or "").strip()
    if not sid:
        return False
    if is_builtin_strategy_id(sid):
        deleted = load_deleted_ids()
        if sid in deleted:
            return False
        deleted.add(sid)
        save_deleted_ids(deleted)
        disabled = load_disabled_ids()
        if sid in disabled:
            disabled.remove(sid)
            save_disabled_ids(disabled)
        return True
    changed = delete_custom_strategy(sid)
    if changed:
        deleted = load_deleted_ids()
        if sid in deleted:
            deleted.remove(sid)
            save_deleted_ids(deleted)
    return changed


def set_strategy_enabled(strategy_id, enabled):
    sid = str(strategy_id or "").strip()
    if not sid:
        raise ValueError("strategy id is required")
    all_ids = {x["id"] for x in list_all_strategy_meta()}
    if sid not in all_ids:
        raise ValueError(f"strategy not found: {sid}")
    disabled = load_disabled_ids()
    if enabled:
        disabled.discard(sid)
    else:
        disabled.add(sid)
    save_disabled_ids(disabled)


def update_custom_strategy(entry):
    sid = str(entry.get("id", "")).strip()
    if not sid:
        raise ValueError("strategy id is required")
    rows = load_custom_strategies()
    idx = -1
    for i, r in enumerate(rows):
        if str(r.get("id", "")).strip() == sid:
            idx = i
            break
    if idx < 0:
        raise ValueError(f"strategy not found: {sid}")
    row = rows[idx]
    if "name" in entry:
        row["name"] = str(entry.get("name", sid)).strip() or sid
    if "class_name" in entry:
        row["class_name"] = str(entry.get("class_name", "")).strip()
    if "code" in entry:
        row["code"] = str(entry.get("code", ""))
        if "kline_type" not in entry:
            row["kline_type"] = infer_kline_type_from_code(row.get("code", ""))
    if "kline_type" in entry:
        row["kline_type"] = normalize_kline_type(entry.get("kline_type"))
    if "analysis_text" in entry:
        row["analysis_text"] = str(entry.get("analysis_text", ""))
    if "raw_requirement" in entry:
        row["raw_requirement"] = str(entry.get("raw_requirement", ""))
    if "raw_requirement_title" in entry:
        row["raw_requirement_title"] = str(entry.get("raw_requirement_title", "")).strip() or "原始需求"
    if "source" in entry:
        source = str(entry.get("source", "")).strip().lower()
        if source in {"human", "market"}:
            row["source"] = source
    if "protect_level" in entry:
        row["protect_level"] = str(entry.get("protect_level", "custom") or "custom").strip() or "custom"
    if "immutable" in entry:
        row["immutable"] = bool(entry.get("immutable", False))
    if "depends_on" in entry:
        row["depends_on"] = _normalize_depends_on(entry.get("depends_on"))
    if "strategy_intent" in entry and isinstance(entry.get("strategy_intent"), dict):
        strategy_intent, intent_explain = normalize_strategy_intent(entry.get("strategy_intent"))
        row["strategy_intent"] = strategy_intent
        row["intent_explain"] = intent_explain
    row["updated_at"] = datetime.now().isoformat(timespec="seconds")
    rows[idx] = row
    save_custom_strategies(rows)
    return True


def instantiate_custom_strategy(entry):
    code = str(entry.get("code", "") or "")
    if not code.strip():
        return None
    class_name = str(entry.get("class_name", "")).strip()
    ns = {
        "BaseImplementedStrategy": BaseImplementedStrategy,
        "Indicators": Indicators,
        "pd": pd,
        "np": np,
        "get_value": get_value
    }
    exec(code, ns, ns)
    if not class_name:
        class_candidates = [
            k for k, v in ns.items()
            if isinstance(v, type) and issubclass(v, BaseImplementedStrategy) and v is not BaseImplementedStrategy
        ]
        if not class_candidates:
            return None
        class_name = class_candidates[0]
    cls = ns.get(class_name)
    if not isinstance(cls, type):
        return None
    inst = cls()
    sid = str(entry.get("id", "")).strip()
    sname = str(entry.get("name", "")).strip()
    if sid:
        inst.id = sid
    if sname:
        inst.name = sname
    return inst
