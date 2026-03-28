import json
import os
from src.utils.config_loader import ConfigLoader
from src.strategies.strategy_manager_repo import list_all_strategy_meta


def _project_root():
    base_dir = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(os.path.dirname(base_dir))


def _base_config_path():
    return os.path.join(_project_root(), "config.json")


def _load_base_config():
    path = _base_config_path()
    if not os.path.exists(path):
        return {}, path
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f), path


def _save_base_config(payload, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)


def _get_path(payload, path, default=None):
    cur = payload
    for key in str(path).split("."):
        if not isinstance(cur, dict) or key not in cur:
            return default
        cur = cur[key]
    return cur


def _set_path(payload, path, value):
    keys = str(path).split(".")
    cur = payload
    for key in keys[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[keys[-1]] = value


def _normalize_symbol(code):
    c = str(code or "").strip().upper()
    if c.endswith(".SH") or c.endswith(".SZ"):
        return c
    if len(c) == 6 and c.isdigit():
        return f"{c}.SH" if c.startswith("6") else f"{c}.SZ"
    return c


def _detect_market(stock_code):
    c = _normalize_symbol(stock_code)
    if c.endswith(".SH") or c.endswith(".SZ"):
        return "A股"
    if c.endswith(".HK"):
        return "港股"
    if c.endswith(".US") or c.endswith(".O"):
        return "美股"
    return "未知"


def _selected_strategy_ids(strategy_id=None, strategy_ids=None, strategy_mode=None):
    if isinstance(strategy_ids, list) and strategy_ids:
        return [str(x).strip() for x in strategy_ids if str(x).strip()]
    if str(strategy_mode or "").strip().lower() == "top5":
        meta = [x for x in list_all_strategy_meta() if bool(x.get("enabled", True))]
        return [str(x.get("id", "")).strip() for x in meta[:5] if str(x.get("id", "")).strip()]
    sid = str(strategy_id or "").strip()
    if sid and sid != "all":
        return [sid]
    return []


def _infer_tags(strategy_id=None, strategy_ids=None, strategy_mode=None):
    ids = set(_selected_strategy_ids(strategy_id=strategy_id, strategy_ids=strategy_ids, strategy_mode=strategy_mode))
    metas = list_all_strategy_meta()
    tags = set()
    for item in metas:
        sid = str(item.get("id", "")).strip()
        if ids and sid not in ids:
            continue
        name = str(item.get("name", "")).strip().lower()
        kline_type = str(item.get("kline_type", "")).strip().lower()
        if any(x in name for x in ["中线", "波段", "长线", "量化", "价值"]):
            tags.update(["中线", "波段", "长线", "量化", "价值"])
        if any(x in name for x in ["短线", "形态", "突破", "均线", "macd"]):
            tags.update(["短线", "形态", "突破", "均线", "MACD"])
        if any(x in name for x in ["超短", "日内", "打板"]):
            tags.update(["超短", "日内", "打板"])
        if kline_type in {"1min", "5min", "10min", "15min"}:
            tags.add("超短")
    return tags


def _rule_match(rule_if, market, tags, supports_t0):
    if not isinstance(rule_if, dict):
        return False
    m = rule_if.get("market")
    if isinstance(m, list) and m:
        if market not in {str(x).strip() for x in m}:
            return False
    m_any = rule_if.get("market_any")
    if isinstance(m_any, list) and m_any:
        if market not in {str(x).strip() for x in m_any}:
            return False
    tags_any = rule_if.get("strategy_tags_any")
    if isinstance(tags_any, list) and tags_any:
        if not ({str(x).strip() for x in tags_any} & set(tags)):
            return False
    if "supports_t0" in rule_if and bool(rule_if.get("supports_t0")) != bool(supports_t0):
        return False
    return True


def _resolve_profile_name(runtime_cfg, stock_code, strategy_id=None, strategy_ids=None, strategy_mode=None):
    baseline = runtime_cfg.get("global_backtest_baseline", {})
    mapping = baseline.get("strategy_profile_mapping", [])
    fallback = str(baseline.get("fallback_profile") or baseline.get("default_profile") or "cn_equity_mid_long").strip()
    market = _detect_market(stock_code)
    tags = _infer_tags(strategy_id=strategy_id, strategy_ids=strategy_ids, strategy_mode=strategy_mode)
    supports_t0 = bool(market in {"港股", "美股"} or "可转债" in tags)
    for item in mapping:
        if not isinstance(item, dict):
            continue
        if _rule_match(item.get("if", {}), market, tags, supports_t0):
            selected = str(item.get("then_profile", "")).strip()
            if selected:
                return selected, market, sorted(tags)
    return fallback, market, sorted(tags)


def apply_backtest_baseline(stock_code, strategy_id=None, strategy_mode=None, strategy_ids=None):
    runtime_cfg = ConfigLoader.reload().to_dict()
    baseline = runtime_cfg.get("global_backtest_baseline", {})
    if not isinstance(baseline, dict) or not bool(baseline.get("enabled", True)):
        return {"applied": False, "reason": "baseline_disabled"}
    profiles = baseline.get("profiles", {})
    if not isinstance(profiles, dict) or not profiles:
        return {"applied": False, "reason": "profiles_empty"}
    profile_name, market, tags = _resolve_profile_name(
        runtime_cfg,
        stock_code=stock_code,
        strategy_id=strategy_id,
        strategy_ids=strategy_ids,
        strategy_mode=strategy_mode
    )
    profile = profiles.get(profile_name)
    if not isinstance(profile, dict):
        return {"applied": False, "reason": "profile_not_found", "profile_name": profile_name}
    base_cfg, path = _load_base_config()
    if not isinstance(base_cfg, dict):
        base_cfg = {}
    before = json.dumps(base_cfg, ensure_ascii=False, sort_keys=True)
    lock_source = bool(baseline.get("lock_backtest_data_source", True))
    fixed_source = str(baseline.get("fixed_data_source", "default") or "default").strip().lower()
    if lock_source and fixed_source in {"default", "tushare", "akshare"}:
        _set_path(base_cfg, "data_provider.source", fixed_source)
        _set_path(base_cfg, "data_provider.enable_fallback", False)
    adjustment_mode = str(profile.get("adjustment_mode", "")).strip()
    if adjustment_mode:
        _set_path(base_cfg, "data_provider.adjustment_mode", adjustment_mode)
    settlement_rule = str(profile.get("settlement_rule", "")).strip()
    if settlement_rule:
        _set_path(base_cfg, "execution.settlement_rule", settlement_rule)
    cost_model = profile.get("cost_model", {})
    if isinstance(cost_model, dict):
        if "commission_rate" in cost_model:
            _set_path(base_cfg, "trading_cost.commission_rate", float(cost_model.get("commission_rate")))
        if "min_commission" in cost_model:
            _set_path(base_cfg, "trading_cost.min_commission", float(cost_model.get("min_commission")))
        if "stamp_duty_sell" in cost_model:
            _set_path(base_cfg, "trading_cost.stamp_duty", float(cost_model.get("stamp_duty_sell")))
        if "slippage" in cost_model:
            _set_path(base_cfg, "execution.slippage", float(cost_model.get("slippage")))
    _set_path(base_cfg, "global_backtest_baseline.last_applied_profile", profile_name)
    _set_path(base_cfg, "global_backtest_baseline.last_applied_market", market)
    after = json.dumps(base_cfg, ensure_ascii=False, sort_keys=True)
    changed = before != after
    if changed:
        _save_base_config(base_cfg, path)
    return {
        "applied": True,
        "changed": changed,
        "profile_name": profile_name,
        "market": market,
        "strategy_tags": tags,
        "adjustment_mode": adjustment_mode,
        "settlement_rule": settlement_rule,
        "data_source": _get_path(base_cfg, "data_provider.source", "default")
    }
