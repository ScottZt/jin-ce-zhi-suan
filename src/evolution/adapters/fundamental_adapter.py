import json
import math
import os
import re
import time
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

import tushare as ts

from src.utils.config_loader import ConfigLoader
from src.utils.tdx_provider import TdxProvider


FUNDAMENTAL_INTERFACE_CATALOG: List[Dict[str, Any]] = [
    {
        "category": "company_profile",
        "label": "公司与行业画像",
        "interfaces": [
            {"key": "stock_basic", "label": "股票基础信息", "api": "stock_basic", "cost_level": "low"},
            {"key": "company", "label": "公司概况", "api": "stock_company", "cost_level": "low"},
            {"key": "concept_detail", "label": "概念题材明细", "api": "concept_detail", "cost_level": "medium"},
        ],
    },
    {
        "category": "valuation_market",
        "label": "估值与市场状态",
        "interfaces": [
            {"key": "daily_basic", "label": "日度估值", "api": "daily_basic", "cost_level": "low"},
            {"key": "moneyflow", "label": "资金流向", "api": "moneyflow", "cost_level": "medium"},
        ],
    },
    {
        "category": "financial_quality",
        "label": "财务质量与安全边际",
        "interfaces": [
            {"key": "fina_indicator", "label": "财务指标", "api": "fina_indicator", "cost_level": "medium"},
            {"key": "income", "label": "利润表", "api": "income", "cost_level": "medium"},
            {"key": "balancesheet", "label": "资产负债表", "api": "balancesheet", "cost_level": "medium"},
            {"key": "cashflow", "label": "现金流量表", "api": "cashflow", "cost_level": "medium"},
            {"key": "dividend", "label": "分红送转", "api": "dividend", "cost_level": "low"},
            {"key": "forecast", "label": "业绩预告", "api": "forecast", "cost_level": "medium"},
            {"key": "express", "label": "业绩快报", "api": "express", "cost_level": "medium"},
        ],
    },
]


def _catalog_map() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for block in FUNDAMENTAL_INTERFACE_CATALOG:
        for item in block.get("interfaces", []):
            key = str(item.get("key", "")).strip()
            if key:
                out[key] = item
    return out


def _normalize_ts_code(stock_code: str) -> str:
    code = str(stock_code or "").strip().upper()
    if not code:
        return ""
    if code.endswith(".SH") or code.endswith(".SZ"):
        return code
    if len(code) == 6 and code.isdigit():
        return f"{code}.SH" if code.startswith("6") else f"{code}.SZ"
    return code


def _safe_file_component(text: str, fallback: str = "unknown") -> str:
    s = str(text or "").strip()
    if not s:
        return fallback
    cleaned = re.sub(r"[^0-9A-Za-z._-]+", "_", s)
    cleaned = cleaned.strip("._-")
    return cleaned[:80] if cleaned else fallback


class FundamentalAdapterManager:
    def __init__(self):
        self._cache: Dict[str, Dict[str, Any]] = {}
        self._last_fetch_ts: Dict[str, float] = {}
        self._catalog_by_key = _catalog_map()

    def _cfg(self) -> Dict[str, Any]:
        cfg = ConfigLoader.reload()
        node = cfg.get("fundamental_adapter", {})
        return node if isinstance(node, dict) else {}

    def _enabled(self, context: str) -> bool:
        node = self._cfg()
        if not bool(node.get("enabled", False)):
            return False
        ctx = str(context or "").strip().lower()
        if ctx == "backtest":
            return bool(node.get("apply_in_backtest", True))
        if ctx == "live":
            return bool(node.get("apply_in_live", True))
        return True

    def _provider_name(self) -> str:
        node = self._cfg()
        return str(node.get("provider", "tushare") or "tushare").strip().lower()

    def _selected_interfaces(self) -> Dict[str, bool]:
        node = self._cfg()
        selected = node.get("tushare_interfaces", {})
        if not isinstance(selected, dict):
            selected = {}
        out: Dict[str, bool] = {}
        for key in self._catalog_by_key.keys():
            out[key] = bool(selected.get(key, False))
        return out

    def _cache_ttl_sec(self) -> int:
        node = self._cfg()
        ttl = int(node.get("cache_ttl_sec", 21600) or 21600)
        return max(60, min(ttl, 7 * 24 * 3600))

    def _min_refresh_sec(self) -> int:
        node = self._cfg()
        v = int(node.get("min_refresh_interval_sec", 900) or 900)
        return max(30, min(v, 12 * 3600))

    def _disk_persist_enabled(self) -> bool:
        node = self._cfg()
        return bool(node.get("disk_persist_enabled", False))

    def _disk_cache_dir(self) -> str:
        node = self._cfg()
        raw = str(node.get("disk_cache_dir", "D:/jin-ce-zhi-suan/data/fundamental_cache") or "D:/jin-ce-zhi-suan/data/fundamental_cache").strip()
        # Ensure Windows path format
        if raw.startswith("/") or raw.startswith("\\wsl"):
            raw = "D:/jin-ce-zhi-suan/data/fundamental_cache"
        return os.path.abspath(raw)

    def _disk_cache_max_files(self) -> int:
        node = self._cfg()
        v = int(node.get("disk_cache_max_files", 300) or 300)
        return max(50, min(v, 5000))

    def prefetch_on_backtest_start(self) -> bool:
        node = self._cfg()
        return bool(node.get("prefetch_on_backtest_start", False))

    def catalog_with_selection(self) -> Dict[str, Any]:
        selected = self._selected_interfaces()
        blocks: List[Dict[str, Any]] = []
        for block in FUNDAMENTAL_INTERFACE_CATALOG:
            interfaces = []
            for item in block.get("interfaces", []):
                key = str(item.get("key", "")).strip()
                x = dict(item)
                x["enabled"] = bool(selected.get(key, False))
                interfaces.append(x)
            blocks.append({
                "category": block.get("category"),
                "label": block.get("label"),
                "interfaces": interfaces,
            })
        return {
            "enabled": bool(self._cfg().get("enabled", False)),
            "provider": self._provider_name(),
            "apply_in_backtest": bool(self._cfg().get("apply_in_backtest", True)),
            "apply_in_live": bool(self._cfg().get("apply_in_live", True)),
            "prefetch_on_backtest_start": self.prefetch_on_backtest_start(),
            "cache_ttl_sec": self._cache_ttl_sec(),
            "min_refresh_interval_sec": self._min_refresh_sec(),
            "disk_persist_enabled": self._disk_persist_enabled(),
            "disk_cache_dir": self._disk_cache_dir(),
            "disk_cache_max_files": self._disk_cache_max_files(),
            "categories": blocks,
        }

    def _trim_disk_cache_files(self, cache_dir: str):
        try:
            max_files = self._disk_cache_max_files()
            files = []
            for name in os.listdir(cache_dir):
                if not str(name).startswith("fundamental_") or not str(name).endswith(".json"):
                    continue
                fp = os.path.join(cache_dir, str(name))
                if os.path.isfile(fp):
                    files.append(fp)
            if len(files) <= max_files:
                return
            files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
            for fp in files[max_files:]:
                try:
                    os.remove(fp)
                except Exception:
                    pass
        except Exception:
            pass

    def _json_safe(self, obj: Any) -> Any:
        def _sanitize(v: Any) -> Any:
            if isinstance(v, float):
                return v if math.isfinite(v) else None
            if isinstance(v, dict):
                out: Dict[str, Any] = {}
                for k, x in v.items():
                    out[str(k)] = _sanitize(x)
                return out
            if isinstance(v, (list, tuple, set)):
                return [_sanitize(x) for x in v]
            if isinstance(v, (str, int, bool)) or v is None:
                return v
            # numpy/pandas scalar fallback
            try:
                fv = float(v)
                if math.isfinite(fv):
                    if isinstance(v, int):
                        return int(v)
                    return fv
                return None
            except Exception:
                pass
            return str(v)

        try:
            sanitized = _sanitize(obj)
            return json.loads(json.dumps(sanitized, ensure_ascii=False, allow_nan=False, default=str))
        except Exception:
            return _sanitize(obj)

    def _to_readable(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        if not isinstance(payload, dict):
            return {}
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        company = summary.get("company_profile") if isinstance(summary.get("company_profile"), dict) else {}
        valuation = summary.get("valuation") if isinstance(summary.get("valuation"), dict) else {}
        quality = summary.get("financial_quality") if isinstance(summary.get("financial_quality"), dict) else {}
        modules = payload.get("modules") if isinstance(payload.get("modules"), dict) else {}
        module_rows: List[Dict[str, Any]] = []
        for key, result in modules.items():
            item = self._catalog_by_key.get(str(key), {})
            r = result if isinstance(result, dict) else {}
            module_rows.append({
                "interface_key": str(key),
                "interface_label": str(item.get("label", key)),
                "status": str(r.get("status", "")),
                "rows": int(r.get("rows", 0) or 0),
                "error_msg": str(r.get("msg", "")),
                "reason_type": str(r.get("reason_type", "")),
                "raw_error": str(r.get("raw_error", "")),
            })
        return {
            "概览": {
                "状态": payload.get("status"),
                "上下文": payload.get("context"),
                "股票": payload.get("ts_code") or payload.get("stock_code"),
                "抓取时间": payload.get("fetched_at"),
                "接口数量": len(module_rows),
            },
            "公司画像": {
                "名称": company.get("name"),
                "行业": company.get("industry"),
                "市场": company.get("market"),
                "上市日期": company.get("list_date"),
            },
            "估值指标": {
                "PE_TTM": valuation.get("pe_ttm"),
                "PB": valuation.get("pb"),
                "PS_TTM": valuation.get("ps_ttm"),
                "股息率_TTM": valuation.get("dv_ttm"),
                "换手率": valuation.get("turnover_rate"),
                "总市值": valuation.get("total_mv"),
            },
            "财务质量": {
                "ROE": quality.get("roe"),
                "ROA": quality.get("roa"),
                "毛利率": quality.get("grossprofit_margin"),
                "资产负债率": quality.get("debt_to_assets"),
                "每股经营现金流": quality.get("ocfps"),
            },
            "接口执行": module_rows,
            "告警": payload.get("warnings") if isinstance(payload.get("warnings"), list) else [],
        }

    def _persist_payload_to_disk(self, payload: Dict[str, Any]):
        if not self._disk_persist_enabled():
            return
        if not isinstance(payload, dict):
            return
        try:
            cache_dir = self._disk_cache_dir()
            os.makedirs(cache_dir, exist_ok=True)
            context = _safe_file_component(str(payload.get("context", "")).lower(), "ctx")
            ts_code = _safe_file_component(str(payload.get("ts_code") or payload.get("stock_code") or ""), "stock")
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            file_name = f"fundamental_{context}_{ts_code}_{stamp}.json"
            file_path = os.path.join(cache_dir, file_name)
            wrapper = {
                "version": 1,
                "saved_at": datetime.now().isoformat(timespec="seconds"),
                "file_name": file_name,
                "payload": payload,
                "readable": self._to_readable(payload),
            }
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(wrapper, f, ensure_ascii=False, indent=2, default=str)
            self._trim_disk_cache_files(cache_dir)
        except Exception:
            pass

    def list_disk_cache(self, stock_code: str = "", context: str = "", limit: int = 60) -> Dict[str, Any]:
        cache_dir = self._disk_cache_dir()
        ctx_filter = str(context or "").strip().lower()
        ts_code_filter = _normalize_ts_code(stock_code) if str(stock_code or "").strip() else ""
        lim = max(1, min(int(limit or 60), 300))
        if not os.path.isdir(cache_dir):
            return {"cache_dir": cache_dir, "exists": False, "items": []}
        files = []
        for name in os.listdir(cache_dir):
            if not str(name).startswith("fundamental_") or not str(name).endswith(".json"):
                continue
            fp = os.path.join(cache_dir, str(name))
            if os.path.isfile(fp):
                files.append(fp)
        files.sort(key=lambda p: os.path.getmtime(p), reverse=True)
        items: List[Dict[str, Any]] = []
        for fp in files:
            if len(items) >= lim:
                break
            try:
                with open(fp, "r", encoding="utf-8") as f:
                    data = json.load(f)
                payload = data.get("payload") if isinstance(data, dict) else {}
                if not isinstance(payload, dict):
                    continue
                one_ctx = str(payload.get("context", "")).strip().lower()
                one_ts_code = str(payload.get("ts_code", "")).strip().upper()
                if ctx_filter and one_ctx != ctx_filter:
                    continue
                if ts_code_filter and one_ts_code != ts_code_filter:
                    continue
                st = os.stat(fp)
                items.append({
                    "file_name": os.path.basename(fp),
                    "path": fp,
                    "size_bytes": int(st.st_size),
                    "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
                    "context": one_ctx,
                    "stock_code": str(payload.get("stock_code", "")),
                    "ts_code": one_ts_code,
                    "status": str(payload.get("status", "")),
                    "fetched_at": str(payload.get("fetched_at", "")),
                    "warning_count": len(payload.get("warnings", [])) if isinstance(payload.get("warnings"), list) else 0,
                    "interfaces_count": len(payload.get("interfaces_enabled", [])) if isinstance(payload.get("interfaces_enabled"), list) else 0,
                })
            except Exception:
                continue
        return {"cache_dir": cache_dir, "exists": True, "items": items}

    def read_disk_cache(self, file_name: str) -> Dict[str, Any]:
        cache_dir = self._disk_cache_dir()
        name = os.path.basename(str(file_name or ""))
        if not name or name != str(file_name or ""):
            return {"status": "error", "msg": "invalid file_name"}
        fp = os.path.join(cache_dir, name)
        if not os.path.isfile(fp):
            return {"status": "error", "msg": "cache file not found"}
        try:
            with open(fp, "r", encoding="utf-8") as f:
                data = json.load(f)
            payload = data.get("payload") if isinstance(data, dict) else {}
            if not isinstance(payload, dict):
                payload = {}
            readable = data.get("readable") if isinstance(data, dict) else {}
            if not isinstance(readable, dict):
                readable = self._to_readable(payload)
            return {
                "status": "success",
                "file_name": name,
                "path": fp,
                "cache_dir": cache_dir,
                "saved_at": data.get("saved_at") if isinstance(data, dict) else "",
                "payload": payload,
                "readable": readable,
            }
        except Exception as e:
            return {"status": "error", "msg": str(e)}

    def _pick_latest_row(self, df) -> Dict[str, Any]:
        if df is None or getattr(df, "empty", True):
            return {}
        try:
            row = df.iloc[0].to_dict()
            return {str(k): row.get(k) for k in row.keys()}
        except Exception:
            return {}

    def _pick_all_rows(self, df) -> List[Dict[str, Any]]:
        if df is None or getattr(df, "empty", True):
            return []
        try:
            rows = df.to_dict(orient="records")
            out: List[Dict[str, Any]] = []
            for row in rows:
                if isinstance(row, dict):
                    out.append({str(k): row.get(k) for k in row.keys()})
            return out
        except Exception:
            return []

    def _call_with_date_fallback(self, api_func, ts_code: str, start_date: str, end_date: str):
        """Try date-window query first, then fallback to ts_code-only when empty."""
        used_fallback = False
        df = api_func(ts_code=ts_code, start_date=start_date, end_date=end_date)
        if df is None or getattr(df, "empty", True):
            used_fallback = True
            df = api_func(ts_code=ts_code)
        return df, used_fallback

    def _classify_error_type(self, raw_text: str) -> str:
        t = str(raw_text or "").strip().lower()
        if not t:
            return "unknown"
        if ("积分" in t) or ("points" in t) or ("insufficient points" in t):
            return "quota_insufficient"
        if ("权限" in t) or ("permission" in t) or ("privilege" in t):
            return "permission_denied"
        if ("频次" in t) or ("频率" in t) or ("rate limit" in t) or ("too many requests" in t):
            return "rate_limited"
        if ("token" in t) or ("您的token不对" in t):
            return "token_invalid"
        return "unknown"

    def _build_error_payload(self, key: str, e: Exception) -> Dict[str, Any]:
        raw_msg = str(e or "").strip()
        args_text = " | ".join([str(x) for x in list(getattr(e, "args", []) or []) if str(x).strip()])
        if args_text and raw_msg not in args_text:
            raw_msg = f"{raw_msg} | args={args_text}" if raw_msg else args_text
        reason = self._classify_error_type(raw_msg)
        if reason == "token_invalid":
            msg = "Token校验失败：请确认Tushare token是否正确。"
        elif reason == "quota_insufficient":
            msg = "积分不足：当前账号积分不足以调用该接口。"
        elif reason == "permission_denied":
            msg = "接口权限不足：当前账号无该接口权限。"
        elif reason == "rate_limited":
            msg = "调用频率受限：请稍后重试或降低请求频率。"
        else:
            msg = "接口调用失败。"
        return {
            "status": "error",
            "interface_key": str(key),
            "msg": msg,
            "reason_type": reason,
            "raw_error": raw_msg or repr(e),
        }

    def _safe_call(self, pro, key: str, ts_code: str, start_date: str, end_date: str) -> Dict[str, Any]:
        try:
            fallback_no_date = False
            if key == "stock_basic":
                df = pro.stock_basic(ts_code=ts_code)
            elif key == "company":
                df = pro.stock_company(ts_code=ts_code)
            elif key == "concept_detail":
                df = pro.concept_detail(ts_code=ts_code)
            elif key == "daily_basic":
                df = pro.daily_basic(ts_code=ts_code, start_date=start_date, end_date=end_date)
            elif key == "moneyflow":
                df = pro.moneyflow(ts_code=ts_code, start_date=start_date, end_date=end_date)
            elif key == "fina_indicator":
                df, fallback_no_date = self._call_with_date_fallback(pro.fina_indicator, ts_code, start_date, end_date)
            elif key == "income":
                df, fallback_no_date = self._call_with_date_fallback(pro.income, ts_code, start_date, end_date)
            elif key == "balancesheet":
                df, fallback_no_date = self._call_with_date_fallback(pro.balancesheet, ts_code, start_date, end_date)
            elif key == "cashflow":
                df, fallback_no_date = self._call_with_date_fallback(pro.cashflow, ts_code, start_date, end_date)
            elif key == "dividend":
                df = pro.dividend(ts_code=ts_code)
            elif key == "forecast":
                df = pro.forecast(ts_code=ts_code)
            elif key == "express":
                df = pro.express(ts_code=ts_code)
            else:
                return {"status": "skipped", "msg": f"unsupported interface: {key}"}
            rows = int(len(df)) if df is not None else 0
            out = {
                "status": "success",
                "rows": rows,
                "sample": self._pick_latest_row(df),
                "records": self._pick_all_rows(df),
            }
            if fallback_no_date:
                out["fallback_no_date"] = True
            return out
        except Exception as e:
            return self._build_error_payload(key, e)

    def _to_float_or_none(self, value: Any) -> Optional[float]:
        """Convert scalar-like value to float, keep None when unavailable."""
        try:
            if value is None:
                return None
            fv = float(value)
            if math.isfinite(fv):
                return fv
            return None
        except Exception:
            return None

    def _build_tdx_summary(self, ts_code: str, latest_bar: Dict[str, Any], daily_rows: List[Dict[str, Any]]) -> Dict[str, Any]:
        """Build a TDX-compatible summary while preserving existing key structure."""
        market = ""
        code_text = str(ts_code or "").upper()
        if code_text.endswith(".SH"):
            market = "SH"
        elif code_text.endswith(".SZ"):
            market = "SZ"
        close_latest = self._to_float_or_none((latest_bar or {}).get("close"))
        close_prev = None
        if len(daily_rows) >= 2:
            close_prev = self._to_float_or_none(daily_rows[-2].get("close"))
        pct_1d = None
        if close_latest is not None and close_prev not in (None, 0):
            try:
                pct_1d = ((close_latest - close_prev) / close_prev) * 100.0
            except Exception:
                pct_1d = None
        return {
            "company_profile": {
                # TDX 基础行情链路不直接提供公司档案，保留字段兼容上层展示。
                "name": None,
                "industry": None,
                "market": market or None,
                "list_date": None,
            },
            "valuation": {
                # 保留 Tushare 同名字段，避免前端/下游读取断裂；TDX 无法直接提供时返回 None。
                "pe_ttm": None,
                "pb": None,
                "ps_ttm": None,
                "dv_ttm": None,
                "turnover_rate": None,
                "total_mv": None,
                "latest_close": close_latest,
                "change_pct_1d": pct_1d,
            },
            "financial_quality": {
                "roe": None,
                "roa": None,
                "grossprofit_margin": None,
                "debt_to_assets": None,
                "ocfps": None,
            },
        }

    def _build_summary(self, outputs: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
        valuation = {}
        db = outputs.get("daily_basic", {})
        dbs = db.get("sample") if isinstance(db, dict) else {}
        if isinstance(dbs, dict):
            valuation = {
                "pe_ttm": dbs.get("pe_ttm"),
                "pb": dbs.get("pb"),
                "ps_ttm": dbs.get("ps_ttm"),
                "dv_ttm": dbs.get("dv_ttm"),
                "turnover_rate": dbs.get("turnover_rate"),
                "total_mv": dbs.get("total_mv"),
            }
        quality = {}
        fi = outputs.get("fina_indicator", {})
        fis = fi.get("sample") if isinstance(fi, dict) else {}
        if isinstance(fis, dict):
            quality = {
                "roe": fis.get("roe"),
                "roa": fis.get("roa"),
                "grossprofit_margin": fis.get("grossprofit_margin"),
                "debt_to_assets": fis.get("debt_to_assets"),
                "ocfps": fis.get("ocfps"),
            }
        profile = {}
        sb = outputs.get("stock_basic", {})
        sbs = sb.get("sample") if isinstance(sb, dict) else {}
        if isinstance(sbs, dict):
            profile = {
                "name": sbs.get("name"),
                "industry": sbs.get("industry"),
                "market": sbs.get("market"),
                "list_date": sbs.get("list_date"),
            }
        return {
            "company_profile": profile,
            "valuation": valuation,
            "financial_quality": quality,
        }

    def get_profile(self, stock_code: str, context: str = "backtest", force: bool = False, allow_network: bool = True) -> Dict[str, Any]:
        ts_code = _normalize_ts_code(stock_code)
        if not ts_code:
            return {"status": "error", "msg": "invalid stock_code"}
        if not self._enabled(context):
            return {"status": "disabled", "msg": "fundamental adapter disabled for current context"}
        provider = self._provider_name()
        if provider not in {"tushare", "tdx"}:
            return {"status": "error", "msg": f"unsupported provider: {provider}"}

        cache_key = f"{context}|{ts_code}"
        now = time.time()
        ttl = self._cache_ttl_sec()
        min_refresh = self._min_refresh_sec()
        cached = self._cache.get(cache_key)
        if isinstance(cached, dict):
            fetched_at = float(cached.get("_fetched_at_ts", 0.0) or 0.0)
            age = now - fetched_at
            if not force and age <= ttl:
                out = self._json_safe(dict(cached))
                if not isinstance(out.get("readable"), dict):
                    out["readable"] = self._to_readable(out)
                out["cache"] = {"hit": True, "age_sec": int(max(0, age)), "ttl_sec": ttl}
                return out
            if (not allow_network) and not force:
                out = self._json_safe(dict(cached))
                if not isinstance(out.get("readable"), dict):
                    out["readable"] = self._to_readable(out)
                out["cache"] = {"hit": True, "age_sec": int(max(0, age)), "ttl_sec": ttl, "stale": age > ttl}
                return out
        last_fetch = float(self._last_fetch_ts.get(cache_key, 0.0) or 0.0)
        if (not force) and (now - last_fetch < min_refresh):
            if isinstance(cached, dict):
                out = self._json_safe(dict(cached))
                if not isinstance(out.get("readable"), dict):
                    out["readable"] = self._to_readable(out)
                out["cache"] = {"hit": True, "age_sec": int(max(0, now - float(cached.get("_fetched_at_ts", now)))), "ttl_sec": ttl}
                out["throttled"] = True
                return out
            return {"status": "throttled", "msg": f"refresh interval not reached: {min_refresh}s"}
        if not allow_network and isinstance(cached, dict):
            out = self._json_safe(dict(cached))
            if not isinstance(out.get("readable"), dict):
                out["readable"] = self._to_readable(out)
            out["cache"] = {"hit": True, "stale": True, "ttl_sec": ttl}
            return out

        outputs: Dict[str, Dict[str, Any]] = {}
        warnings: List[str] = []
        enabled_keys: List[str] = []
        summary: Dict[str, Any] = {}

        if provider == "tushare":
            cfg = ConfigLoader.reload()
            token = str(cfg.get("data_provider.tushare_token", "") or "").strip()
            if not token:
                return {"status": "error", "msg": "tushare_token 未配置"}
            ts.set_token(token)
            pro = ts.pro_api()
            selected = self._selected_interfaces()
            enabled_keys = [k for k, v in selected.items() if bool(v)]
            if not enabled_keys:
                return {"status": "disabled", "msg": "未勾选任何 Tushare 基本面接口"}
            end_date = datetime.now().strftime("%Y%m%d")
            start_date = (datetime.now() - timedelta(days=480)).strftime("%Y%m%d")
            for key in enabled_keys:
                ret = self._safe_call(pro, key=key, ts_code=ts_code, start_date=start_date, end_date=end_date)
                outputs[key] = ret
                if ret.get("status") == "error":
                    raw = str(ret.get("raw_error", "") or "")
                    base = f"{key}: {ret.get('msg', '')}"
                    warnings.append(f"{base} | raw={raw}" if raw else base)
            summary = self._build_summary(outputs)
        else:
            # TDX 目前仅补充行情侧“基本面近似信息”（最新价、近日日线变化等）。
            # 返回结构保持与 Tushare 分支一致，避免上游读取逻辑发生回归。
            enabled_keys = ["tdx_latest_bar", "tdx_daily_bars"]
            tdx = TdxProvider()
            now_dt = datetime.now().replace(second=0, microsecond=0)
            st_dt = now_dt - timedelta(days=480)
            latest_bar = tdx.get_latest_bar(ts_code)
            if isinstance(latest_bar, dict) and latest_bar:
                outputs["tdx_latest_bar"] = {
                    "status": "success",
                    "rows": 1,
                    "sample": {str(k): latest_bar.get(k) for k in latest_bar.keys()},
                    "records": [{str(k): latest_bar.get(k) for k in latest_bar.keys()}],
                }
            else:
                msg = str(tdx.last_error or "TDX latest bar unavailable")
                outputs["tdx_latest_bar"] = {
                    "status": "error",
                    "rows": 0,
                    "msg": msg,
                    "reason_type": "tdx_data_unavailable",
                    "raw_error": msg,
                }
                warnings.append(f"tdx_latest_bar: {msg}")
            daily_df = tdx.fetch_kline_data(ts_code, st_dt, now_dt, interval="D")
            daily_records = self._pick_all_rows(daily_df)
            if daily_records:
                outputs["tdx_daily_bars"] = {
                    "status": "success",
                    "rows": int(len(daily_records)),
                    "sample": dict(daily_records[-1]),
                    "records": daily_records,
                }
            else:
                msg = str(tdx.last_error or "TDX daily bars unavailable")
                outputs["tdx_daily_bars"] = {
                    "status": "error",
                    "rows": 0,
                    "msg": msg,
                    "reason_type": "tdx_data_unavailable",
                    "raw_error": msg,
                }
                warnings.append(f"tdx_daily_bars: {msg}")
            summary = self._build_tdx_summary(
                ts_code=ts_code,
                latest_bar=outputs.get("tdx_latest_bar", {}).get("sample", {}),
                daily_rows=daily_records,
            )

        payload: Dict[str, Any] = {
            "status": "success" if outputs else "empty",
            "provider": provider,
            "context": str(context),
            "stock_code": str(stock_code),
            "ts_code": ts_code,
            "fetched_at": datetime.now().isoformat(timespec="seconds"),
            "interfaces_enabled": enabled_keys,
            "modules": outputs,
            "summary": summary,
            "warnings": warnings[:8],
            "_fetched_at_ts": now,
            "cache": {"hit": False, "ttl_sec": ttl},
        }
        payload = self._json_safe(payload)
        payload["readable"] = self._to_readable(payload)
        self._cache[cache_key] = payload
        self._last_fetch_ts[cache_key] = now
        self._persist_payload_to_disk(payload)
        return self._json_safe(dict(payload))
