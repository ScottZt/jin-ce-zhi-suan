
import asyncio
import json
import os
import importlib
import sys
import math
import numbers
import re
import urllib.request
import urllib.error
import io
import pandas as pd
import numpy as np
import matplotlib
matplotlib.use("Agg")
import matplotlib.lines as mlines
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, Response, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from src.core.live_cabinet import LiveCabinet
from src.core.backtest_cabinet import BacktestCabinet
from src.utils.config_loader import ConfigLoader
import src.strategies.strategy_factory as strategy_factory_module
from src.strategies.strategy_manager_repo import (
    list_all_strategy_meta,
    next_custom_strategy_id,
    build_fallback_strategy_code,
    add_custom_strategy,
    update_custom_strategy,
    delete_strategy,
    set_strategy_enabled
)
from src.strategy_intent.intent_engine import StrategyIntentEngine
from src.utils.stock_manager import stock_manager
from src.utils.data_provider import DataProvider
from src.utils.tushare_provider import TushareProvider
from src.utils.akshare_provider import AkshareProvider
from src.utils.history_sync_service import HistoryDiffSyncService, TABLE_INTERVAL_MAP

import logging

# Configure Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger("CabinetServer")

app = FastAPI(title="三省六部 AI 交易决策控制台")

@app.middleware("http")
async def log_requests(request, call_next):
    logger.info(f"Incoming Request: {request.method} {request.url.path}")
    response = await call_next(request)
    logger.info(f"Response Status: {response.status_code}")
    return response

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Global variables
active_connections = []
cabinet_task = None
current_cabinet = None
current_provider_source = None
latest_backtest_result = None
latest_strategy_reports = {}
current_backtest_report = None
current_backtest_progress = {"progress": 0, "current_date": None}
current_backtest_trades = []
kline_daily_cache = {}
report_strategy_kline_cache = {}
report_ai_review_cache = {}
strategy_score_cache = {}
report_detail_cache = {}
report_history_mtime = None
AI_REVIEW_SCHEMA_VERSION = 2
report_history = []
REPORTS_DIR = os.path.join("data", "reports")
REPORTS_FILE = os.path.join(REPORTS_DIR, "backtest_reports.json")
PATTERN_THUMB_DIR = os.path.join(REPORTS_DIR, "pattern_thumbs")
CLASSIC_PATTERN_ITEMS = [
    {"stock": "688585", "start": "2025-07-09", "end": "2025-12-31"},
    {"stock": "301030", "start": "2025-01-02", "end": "2025-06-30"},
    {"stock": "600376", "start": "2025-07-01", "end": "2025-12-31"},
    {"stock": "601888", "start": "2025-01-02", "end": "2025-06-30"},
    {"stock": "300450", "start": "2025-04-01", "end": "2025-09-30"},
    {"stock": "603083", "start": "2025-03-03", "end": "2025-08-29"},
    {"stock": "600941", "start": "2025-07-01", "end": "2025-12-31"},
    {"stock": "601857", "start": "2025-01-02", "end": "2025-06-30"},
    {"stock": "300118", "start": "2025-06-02", "end": "2025-11-28"},
    {"stock": "002475", "start": "2025-09-01", "end": "2025-12-31"}
]

# Config
config = ConfigLoader()
intent_engine = StrategyIntentEngine()
history_sync_service = HistoryDiffSyncService()
history_sync_scheduler_task = None

def is_live_enabled():
    cfg = ConfigLoader.reload()
    return bool(cfg.get("system.enable_live", True))

def load_report_history(force=False):
    global report_history, latest_backtest_result, latest_strategy_reports, report_history_mtime, report_detail_cache
    os.makedirs(REPORTS_DIR, exist_ok=True)
    if not os.path.exists(REPORTS_FILE):
        report_history = []
        report_history_mtime = None
        report_detail_cache = {}
        return
    try:
        mtime = os.path.getmtime(REPORTS_FILE)
        if (not force) and (report_history_mtime is not None) and (abs(float(mtime) - float(report_history_mtime)) < 1e-9):
            return
        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        report_history = payload.get("reports", [])
        report_history_mtime = float(mtime)
        report_detail_cache = {}
        if report_history:
            latest = report_history[0]
            latest_backtest_result = latest.get("summary")
            latest_strategy_reports = latest.get("strategy_reports", {})
        _rebuild_strategy_score_cache()
    except Exception as e:
        logger.error(f"Failed to load report history: {e}")
        report_history = []
        report_history_mtime = None
        report_detail_cache = {}
        _rebuild_strategy_score_cache()

def persist_report_history():
    global report_history_mtime, report_detail_cache
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"reports": report_history}, f, ensure_ascii=False, indent=2, default=str)
    report_history_mtime = os.path.getmtime(REPORTS_FILE) if os.path.exists(REPORTS_FILE) else None
    report_detail_cache = {}


def _score_grade(score):
    s = float(score or 0.0)
    if s >= 90:
        return "S"
    if s >= 75:
        return "A"
    if s >= 60:
        return "B"
    return "C"


def _sample_size_penalty_points(count):
    c = int(count or 0)
    if c >= 12:
        return 0.0
    if c >= 8:
        return 1.0
    if c >= 5:
        return 3.0
    if c >= 3:
        return 5.0
    if c >= 2:
        return 8.0
    return 12.0


def _sample_size_confidence(count):
    c = int(count or 0)
    if c >= 12:
        return 1.0
    if c >= 8:
        return 0.9
    if c >= 5:
        return 0.75
    if c >= 3:
        return 0.6
    if c >= 2:
        return 0.45
    return 0.3


def _rebuild_strategy_score_cache():
    global strategy_score_cache
    stats = {}
    for rep in report_history if isinstance(report_history, list) else []:
        if not isinstance(rep, dict):
            continue
        summary = rep.get("summary") if isinstance(rep.get("summary"), dict) else {}
        ranking = summary.get("ranking", []) if isinstance(summary, dict) else []
        if not isinstance(ranking, list):
            continue
        for row in ranking:
            if not isinstance(row, dict):
                continue
            sid = str(row.get("strategy_id", "")).strip()
            if not sid:
                continue
            score = row.get("score_total", None)
            if not isinstance(score, numbers.Number):
                continue
            score = float(score)
            annual = float(row.get("annualized_roi", 0.0) or 0.0)
            dd = float(row.get("max_dd", 0.0) or 0.0)
            tr = float(row.get("total_trades", 0.0) or 0.0)
            x = stats.get(sid)
            if x is None:
                stats[sid] = {
                    "count": 1,
                    "score_sum": score,
                    "annual_sum": annual,
                    "dd_sum": dd,
                    "trades_sum": tr,
                    "score_total_latest": score,
                    "rating_latest": str(row.get("rating", "")).strip() or _score_grade(score)
                }
            else:
                x["count"] += 1
                x["score_sum"] += score
                x["annual_sum"] += annual
                x["dd_sum"] += dd
                x["trades_sum"] += tr
    out = {}
    for sid, x in stats.items():
        cnt = max(1, int(x.get("count", 1)))
        avg_score = float(x.get("score_sum", 0.0)) / cnt
        penalty = _sample_size_penalty_points(cnt)
        confidence = _sample_size_confidence(cnt)
        adjusted = max(0.0, avg_score - penalty)
        out[sid] = {
            "score_total": avg_score,
            "rating": _score_grade(adjusted),
            "score_total_adjusted": adjusted,
            "score_penalty_points": penalty,
            "score_confidence": confidence,
            "score_backtest_count": cnt,
            "score_total_latest": float(x.get("score_total_latest", 0.0)),
            "rating_latest": str(x.get("rating_latest", "C")),
            "score_annualized_roi_avg": float(x.get("annual_sum", 0.0)) / cnt,
            "score_max_dd_avg": float(x.get("dd_sum", 0.0)) / cnt,
            "score_trades_avg": float(x.get("trades_sum", 0.0)) / cnt
        }
    strategy_score_cache = out

def _safe_json_obj(obj):
    try:
        return json.loads(json.dumps(obj, ensure_ascii=False, default=str))
    except Exception:
        return None

def _sanitize_non_finite(obj):
    if isinstance(obj, dict):
        return {k: _sanitize_non_finite(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize_non_finite(v) for v in obj]
    if isinstance(obj, tuple):
        return [_sanitize_non_finite(v) for v in obj]
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, numbers.Integral):
        return int(obj)
    if isinstance(obj, numbers.Real):
        v = float(obj)
        return v if math.isfinite(v) else 0.0
    return obj

def start_new_backtest_report(stock_code, strategy_id, request_payload=None):
    global current_backtest_report, latest_backtest_result, latest_strategy_reports, current_backtest_progress, current_backtest_trades
    report_id = f"{int(datetime.now().timestamp() * 1000)}-{os.urandom(2).hex()}"
    current_backtest_report = {
        "report_id": report_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stock_code": stock_code,
        "strategy_id": strategy_id,
        "status": "running",
        "error_msg": None,
        "request": request_payload if isinstance(request_payload, dict) else {},
        "summary": None,
        "ranking": [],
        "strategy_reports": {}
    }
    latest_backtest_result = None
    latest_strategy_reports = {}
    current_backtest_progress = {"progress": 0, "current_date": None}
    current_backtest_trades = []
    return report_id

def finalize_current_backtest_report():
    global report_history, current_backtest_report
    if not current_backtest_report:
        return
    if not current_backtest_report.get("finished_at"):
        current_backtest_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    report_history = [r for r in report_history if r.get("report_id") != current_backtest_report.get("report_id")]
    report_history.insert(0, current_backtest_report)
    persist_report_history()
    _rebuild_strategy_score_cache()

def fail_current_backtest_report(msg):
    global current_backtest_report
    if not current_backtest_report:
        return
    current_backtest_report["status"] = "failed"
    current_backtest_report["error_msg"] = str(msg)
    current_backtest_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    finalize_current_backtest_report()

def cancel_current_backtest_report(msg="backtest cancelled"):
    global current_backtest_report
    if not current_backtest_report:
        return
    current_backtest_report["status"] = "cancelled"
    current_backtest_report["error_msg"] = str(msg)
    current_backtest_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
    finalize_current_backtest_report()

# --- WebSocket Manager ---
async def connect(websocket: WebSocket):
    await websocket.accept()
    active_connections.append(websocket)

def disconnect(websocket: WebSocket):
    active_connections.remove(websocket)

async def broadcast(message: dict):
    # print(f"Broadcasting: {message}")
    for connection in active_connections:
        try:
            await connection.send_json(message)
        except Exception:
            pass

# --- Event Callback for LiveCabinet ---
async def cabinet_event_handler(event_type, data):
    """
    Bridge between LiveCabinet and WebSocket clients.
    """
    payload = {
        "type": event_type,
        "data": data,
        "timestamp": asyncio.get_event_loop().time()
    }
    await broadcast(payload)

# --- Models for API ---
class BacktestRequest(BaseModel):
    stock_code: str = "600036.SH"
    strategy_id: str = "all"
    strategy_ids: Optional[list[str]] = None
    strategy_mode: Optional[str] = None
    start: Optional[str] = None
    end: Optional[str] = None
    capital: Optional[float] = None

class LiveRequest(BaseModel):
    stock_code: str = "600036.SH"

class StrategySwitchRequest(BaseModel):
    strategy_id: Optional[str] = None
    strategy_ids: Optional[list[str]] = None

class SourceSwitchRequest(BaseModel):
    source: str

class ConfigUpdateRequest(BaseModel):
    config: dict

class StrategyToggleRequest(BaseModel):
    strategy_id: str
    enabled: bool

class StrategyAnalyzeRequest(BaseModel):
    template_text: str
    strategy_name: Optional[str] = None
    code_template: Optional[str] = None
    kline_type: Optional[str] = None


class StrategyMarketAnalyzeRequest(BaseModel):
    market_state: dict
    strategy_name: Optional[str] = None
    code_template: Optional[str] = None
    kline_type: Optional[str] = None

class StrategyAddRequest(BaseModel):
    strategy_id: str
    strategy_name: str
    class_name: Optional[str] = None
    code: str
    template_text: Optional[str] = None
    analysis_text: Optional[str] = None
    strategy_intent: Optional[dict] = None
    source: Optional[str] = None
    kline_type: Optional[str] = None
    raw_requirement_title: Optional[str] = None
    raw_requirement: Optional[str] = None


class StrategyUpdateRequest(BaseModel):
    strategy_id: str
    strategy_name: Optional[str] = None
    class_name: Optional[str] = None
    code: Optional[str] = None
    analysis_text: Optional[str] = None
    source: Optional[str] = None
    kline_type: Optional[str] = None
    raw_requirement_title: Optional[str] = None
    raw_requirement: Optional[str] = None

class StrategyDeleteRequest(BaseModel):
    strategy_id: str

class ReportDeleteRequest(BaseModel):
    report_id: str

class HistorySyncRunRequest(BaseModel):
    codes: Optional[list[str]] = None
    tables: Optional[list[str]] = None
    start_time: Optional[str] = None
    end_time: Optional[str] = None
    lookback_days: int = 10
    max_codes: int = 200
    batch_size: int = 500
    dry_run: bool = False
    on_duplicate: str = "ignore"
    async_run: bool = False

class HistorySyncScheduleRequest(BaseModel):
    interval_minutes: int = 60
    lookback_days: int = 10
    max_codes: int = 200
    batch_size: int = 500
    tables: Optional[list[str]] = None
    dry_run: bool = False
    on_duplicate: str = "ignore"


def _extract_code_block(text):
    m = re.search(r"```python\s*([\s\S]*?)```", str(text or ""), re.IGNORECASE)
    if m:
        return m.group(1).strip()
    m2 = re.search(r"```\s*([\s\S]*?)```", str(text or ""), re.IGNORECASE)
    return m2.group(1).strip() if m2 else str(text or "").strip()


def _extract_first_class_name(code_text):
    m = re.search(r"class\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", str(code_text or ""))
    return m.group(1) if m else ""


def _normalize_kline_type(value):
    v = str(value or "").strip()
    if not v:
        return "1min"
    return v


def _apply_kline_type_to_code(code_text, kline_type):
    code = str(code_text or "")
    tf = _normalize_kline_type(kline_type)
    pattern = r"trigger_timeframe\s*=\s*['\"][^'\"]+['\"]"
    if re.search(pattern, code):
        return re.sub(pattern, f'trigger_timeframe="{tf}"', code, count=1)
    super_pattern = r"super\(\)\.__init__\((.*?)\)"
    m = re.search(super_pattern, code, flags=re.DOTALL)
    if not m:
        return code
    args_text = str(m.group(1) or "")
    if "trigger_timeframe" in args_text:
        return code
    new_args = args_text.strip()
    if new_args:
        new_args = f"{new_args}, trigger_timeframe=\"{tf}\""
    else:
        new_args = f"trigger_timeframe=\"{tf}\""
    return code[:m.start(1)] + new_args + code[m.end(1):]


def _build_ai_analysis(strategy_intent, strategy_id, strategy_name, code_template=None):
    intent_obj = intent_engine.normalize(strategy_intent)
    intent = intent_obj.to_dict()
    intent_explain = intent_obj.explain()
    cfg = ConfigLoader.reload()
    api_key = str(
        cfg.get("data_provider.strategy_llm_api_key", "")
        or cfg.get("data_provider.llm_api_key", "")
        or cfg.get("data_provider.api_key", "")
        or cfg.get("data_provider.default_api_key", "")
        or ""
    ).strip()
    base_url = str(
        cfg.get("data_provider.strategy_llm_api_url", "")
        or cfg.get("data_provider.llm_api_url", "")
        or cfg.get("data_provider.default_api_url", "")
        or ""
    ).strip()
    model_name = str(
        cfg.get("data_provider.strategy_llm_model", "")
        or cfg.get("data_provider.llm_model", "")
        or ""
    ).strip() or "gpt-4o-mini"
    timeout_sec = int(
        cfg.get("data_provider.strategy_llm_timeout_sec", 0)
        or cfg.get("data_provider.llm_timeout_sec", 0)
        or 120
    )
    timeout_sec = max(30, min(timeout_sec, 300))
    strategy_name = str(strategy_name or f"AI策略{strategy_id}").strip()
    fallback_code = build_fallback_strategy_code(strategy_id, strategy_name, intent_explain)
    fallback_class_name = _extract_first_class_name(fallback_code)
    if not api_key or not base_url:
        return {
            "analysis_text": "未检测到可用大模型配置，已返回可执行默认策略代码。",
            "code": fallback_code,
            "class_name": fallback_class_name,
            "strategy_intent": intent,
            "intent_explain": intent_explain
        }
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        if url.endswith("/v1"):
            url = f"{url}/chat/completions"
        else:
            url = f"{url}/v1/chat/completions"
    system_prompt = (
        "你是资深量化开发专家。你只能根据StrategyIntent生成策略代码，禁止基于原始自然语言直接生成代码。"
        "只生成一个类，继承BaseImplementedStrategy，类中必须实现on_bar。"
        "必须遵守A股基础交易规则并在代码中显式实现："
        "1) T+1：当日买入不得当日卖出，需记录last_buy_day并拦截所有SELL/止损/止盈路径；"
        "2) 涨跌停：接近涨停禁止追高买入；跌停或接近跌停不得卖出，需pending_sell次日重试；"
        "3) 停牌与异常数据：volume<=0或close<=0或high<low直接跳过；"
        "4) 交易单位：买卖数量必须100股整数倍，不足100不下单；"
        "5) 重复开仓限制：已有仓位不得重复买入；"
        "6) 时间窗：明确买入窗口与卖出窗口，窗口外不交易；"
        "7) 风控优先级：强制止损/风险退出优先于普通信号；"
        "8) 代码健壮性：指标输入必须数值化处理，避免None/字符串导致运行时异常。"
    )
    user_prompt = (
        f"策略ID固定为: {strategy_id}\n"
        f"策略名称固定为: {strategy_name}\n"
        f"StrategyIntent(JSON)：\n{json.dumps(intent, ensure_ascii=False, indent=2)}\n\n"
        f"Intent解释：{intent_explain}\n\n"
        "基础约束补充：A股T+1、涨跌停限制、停牌与异常数据过滤、100股整手、已有仓位禁止重复买入、"
        "交易时间窗、强制风控优先、pending_sell重试机制，必须全部落地到代码。\n\n"
        f"请尽量遵循以下代码骨架与风格约束：\n{str(code_template or '').strip()}\n\n"
        "返回格式：先给Intent可解释性说明，再给```python```代码块。代码需可直接运行于当前项目。"
    )
    payload = {
        "model": model_name,
        "temperature": 0.2,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }
    try:
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
        result = json.loads(raw)
        content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
        code = _extract_code_block(content)
        class_name = _extract_first_class_name(code)
        if not code or not class_name:
            return {
                "analysis_text": "大模型返回内容未包含可执行代码，已回退默认策略代码。",
                "code": fallback_code,
                "class_name": fallback_class_name,
                "strategy_intent": intent,
                "intent_explain": intent_explain
            }
        analysis_text = re.sub(r"```[\s\S]*?```", "", str(content or "")).strip()
        if not analysis_text:
            analysis_text = "已完成策略分析并生成可执行代码。"
        return {
            "analysis_text": analysis_text,
            "code": code,
            "class_name": class_name,
            "strategy_intent": intent,
            "intent_explain": intent_explain
        }
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", errors="ignore")[:240]
        except Exception:
            detail = ""
        msg = f"大模型分析调用失败（HTTP {int(e.code)}）"
        if detail:
            msg = f"{msg}：{detail}"
        return {
            "analysis_text": f"{msg}，已回退默认策略代码。",
            "code": fallback_code,
            "class_name": fallback_class_name,
            "strategy_intent": intent,
            "intent_explain": intent_explain
        }
    except Exception as e:
        err = str(e).strip()
        msg = f"大模型分析调用失败（{type(e).__name__}）"
        if err:
            msg = f"{msg}：{err[:200]}"
        return {
            "analysis_text": f"{msg}，已回退默认策略代码。",
            "code": fallback_code,
            "class_name": fallback_class_name,
            "strategy_intent": intent,
            "intent_explain": intent_explain
        }

# --- Routes ---

@app.get("/")
async def get_dashboard():
    html = open("dashboard.html", "r", encoding="utf-8").read()
    live_enabled_flag = "true" if is_live_enabled() else "false"
    html = html.replace(
        "<!-- JavaScript Logic -->",
        f"<script>window.__LIVE_ENABLED__ = {live_enabled_flag};</script>\n    <!-- JavaScript Logic -->",
        1
    )
    return HTMLResponse(content=html)

@app.get("/report")
async def get_report_page():
    return HTMLResponse(content=open("backtest_report.html", "r", encoding="utf-8").read())


@app.get("/logo.png")
async def get_logo():
    logo_path = os.path.abspath("logo.png")
    if not os.path.exists(logo_path):
        raise HTTPException(status_code=404, detail="logo not found")
    return FileResponse(
        logo_path,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=31536000, immutable"}
    )

@app.get("/api/search")
async def search_stocks(q: str = ""):
    """Search stocks by code, name, or pinyin"""
    return {"results": stock_manager.search(q)}

@app.get("/api/strategies")
async def api_strategies():
    try:
        strategies = strategy_factory_module.create_strategies()
        return {
            "status": "success",
            "strategies": [{"id": s.id, "name": s.name} for s in strategies]
        }
    except Exception as e:
        logger.error(f"Failed to load strategies: {e}", exc_info=True)
        return {"status": "error", "strategies": []}


@app.get("/api/strategy_manager/list")
async def api_strategy_manager_list():
    try:
        rows = list_all_strategy_meta()
        out = []
        for row in rows:
            sid = str(row.get("id", "")).strip()
            item = dict(row)
            sc = strategy_score_cache.get(sid, {})
            item["score_total"] = sc.get("score_total", None)
            item["rating"] = sc.get("rating", "")
            item["score_total_adjusted"] = sc.get("score_total_adjusted", None)
            item["score_penalty_points"] = sc.get("score_penalty_points", 0.0)
            item["score_confidence"] = sc.get("score_confidence", 0.0)
            item["score_backtest_count"] = sc.get("score_backtest_count", 0)
            item["score_total_latest"] = sc.get("score_total_latest", None)
            item["rating_latest"] = sc.get("rating_latest", "")
            item["score_annualized_roi_avg"] = sc.get("score_annualized_roi_avg", 0.0)
            item["score_max_dd_avg"] = sc.get("score_max_dd_avg", 0.0)
            item["score_trades_avg"] = sc.get("score_trades_avg", 0.0)
            out.append(item)
        return {"status": "success", "strategies": out}
    except Exception as e:
        logger.error(f"/api/strategy_manager/list failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e), "strategies": []}

@app.get("/api/strategy_manager/detail")
async def api_strategy_manager_detail(strategy_id: str):
    try:
        sid = str(strategy_id or "").strip()
        if not sid:
            return {"status": "error", "msg": "strategy_id is required"}
        rows = list_all_strategy_meta()
        target = None
        for row in rows:
            row_id = str(row.get("id", "")).strip()
            if row_id == sid:
                target = dict(row)
                break
        if target is None:
            return {"status": "not_found", "msg": f"strategy {sid} not found"}
        sc = strategy_score_cache.get(sid, {})
        target["score_total"] = sc.get("score_total", None)
        target["rating"] = sc.get("rating", "")
        target["score_total_adjusted"] = sc.get("score_total_adjusted", None)
        target["score_penalty_points"] = sc.get("score_penalty_points", 0.0)
        target["score_confidence"] = sc.get("score_confidence", 0.0)
        target["score_backtest_count"] = sc.get("score_backtest_count", 0)
        target["score_total_latest"] = sc.get("score_total_latest", None)
        target["rating_latest"] = sc.get("rating_latest", "")
        target["score_annualized_roi_avg"] = sc.get("score_annualized_roi_avg", 0.0)
        target["score_max_dd_avg"] = sc.get("score_max_dd_avg", 0.0)
        target["score_trades_avg"] = sc.get("score_trades_avg", 0.0)
        return {"status": "success", "strategy": target}
    except Exception as e:
        logger.error(f"/api/strategy_manager/detail failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e), "strategy": None}


@app.post("/api/strategy_manager/toggle")
async def api_strategy_manager_toggle(req: StrategyToggleRequest):
    try:
        set_strategy_enabled(req.strategy_id, req.enabled)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"/api/strategy_manager/toggle failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.post("/api/strategy_manager/analyze")
async def api_strategy_manager_analyze(req: StrategyAnalyzeRequest):
    strategy_id = next_custom_strategy_id()
    strategy_name = str(req.strategy_name or f"AI策略{strategy_id}").strip()
    intent = intent_engine.from_human_input(req.template_text)
    result = _build_ai_analysis(intent.to_dict(), strategy_id, strategy_name, req.code_template)
    kline_type = _normalize_kline_type(req.kline_type)
    code_text = _apply_kline_type_to_code(result.get("code", ""), kline_type)
    return {
        "status": "success",
        "source": "human",
        "intent_stage": "中书省前置层",
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "kline_type": kline_type,
        "strategy_intent": result.get("strategy_intent", {}),
        "intent_explain": result.get("intent_explain", ""),
        "analysis_text": result.get("analysis_text", ""),
        "code": code_text,
        "class_name": result.get("class_name", ""),
        "cabinet_flow": ["中书省前置层(Intent)", "中书省(策略生成)", "门下省(风控)", "尚书省(执行)"]
    }


@app.post("/api/strategy_manager/analyze_market")
async def api_strategy_manager_analyze_market(req: StrategyMarketAnalyzeRequest):
    strategy_id = next_custom_strategy_id()
    strategy_name = str(req.strategy_name or f"市场驱动策略{strategy_id}").strip()
    intent = intent_engine.from_market_analysis(req.market_state)
    result = _build_ai_analysis(intent.to_dict(), strategy_id, strategy_name, req.code_template)
    kline_type = _normalize_kline_type(req.kline_type)
    code_text = _apply_kline_type_to_code(result.get("code", ""), kline_type)
    return {
        "status": "success",
        "source": "market",
        "intent_stage": "中书省前置层",
        "strategy_id": strategy_id,
        "strategy_name": strategy_name,
        "kline_type": kline_type,
        "strategy_intent": result.get("strategy_intent", {}),
        "intent_explain": result.get("intent_explain", ""),
        "analysis_text": result.get("analysis_text", ""),
        "code": code_text,
        "class_name": result.get("class_name", ""),
        "cabinet_flow": ["中书省前置层(Intent)", "中书省(策略生成)", "门下省(风控)", "尚书省(执行)"]
    }


@app.post("/api/strategy_manager/add")
async def api_strategy_manager_add(req: StrategyAddRequest):
    try:
        kline_type = _normalize_kline_type(req.kline_type)
        code_text = _apply_kline_type_to_code(req.code, kline_type)
        class_name = _extract_first_class_name(code_text) or (req.class_name or "")
        strategy_intent = req.strategy_intent
        if not isinstance(strategy_intent, dict):
            strategy_intent = intent_engine.from_human_input(req.template_text or req.analysis_text or req.strategy_name).to_dict()
        add_custom_strategy({
            "id": req.strategy_id,
            "name": req.strategy_name,
            "class_name": class_name,
            "code": code_text,
            "template_text": req.template_text or "",
            "analysis_text": req.analysis_text or "",
            "strategy_intent": strategy_intent,
            "source": req.source or "",
            "kline_type": kline_type,
            "raw_requirement_title": req.raw_requirement_title or "",
            "raw_requirement": req.raw_requirement or ""
        })
        return {"status": "success"}
    except Exception as e:
        logger.error(f"/api/strategy_manager/add failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.post("/api/strategy_manager/update")
async def api_strategy_manager_update(req: StrategyUpdateRequest):
    try:
        payload = {"id": req.strategy_id}
        if req.strategy_name is not None:
            payload["name"] = req.strategy_name
        if req.class_name is not None:
            payload["class_name"] = req.class_name
        if req.code is not None:
            code_text = req.code
            if req.kline_type is not None:
                code_text = _apply_kline_type_to_code(code_text, req.kline_type)
            payload["code"] = code_text
            if not req.class_name:
                payload["class_name"] = _extract_first_class_name(code_text)
        if req.analysis_text is not None:
            payload["analysis_text"] = req.analysis_text
        if req.source is not None:
            payload["source"] = req.source
        if req.kline_type is not None:
            payload["kline_type"] = _normalize_kline_type(req.kline_type)
        if req.raw_requirement_title is not None:
            payload["raw_requirement_title"] = req.raw_requirement_title
        if req.raw_requirement is not None:
            payload["raw_requirement"] = req.raw_requirement
        update_custom_strategy(payload)
        return {"status": "success"}
    except Exception as e:
        logger.error(f"/api/strategy_manager/update failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.post("/api/strategy_manager/delete")
async def api_strategy_manager_delete(req: StrategyDeleteRequest):
    try:
        deleted = delete_strategy(req.strategy_id)
        return {"status": "success" if deleted else "info", "deleted": bool(deleted)}
    except Exception as e:
        logger.error(f"/api/strategy_manager/delete failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}

@app.get("/api/config")
async def api_get_config():
    try:
        cfg = ConfigLoader.reload()
        payload = cfg.to_dict()
        return {"status": "success", "config": payload}
    except Exception as e:
        logger.error(f"/api/config failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e), "config": {}}

@app.post("/api/config/save")
async def api_save_config(req: ConfigUpdateRequest):
    global config, cabinet_task, current_cabinet, current_provider_source
    try:
        if not isinstance(req.config, dict):
            return {"status": "error", "msg": "config must be object"}
        cfg = ConfigLoader.reload()
        cfg._config = req.config
        cfg.save("config.json")
        config = ConfigLoader.reload()
        current_provider_source = config.get("data_provider.source", "default")
        live_enabled = bool(config.get("system.enable_live", True))
        restarted = False
        if current_cabinet and type(current_cabinet).__name__ == "LiveCabinet":
            stock_code = getattr(current_cabinet, "stock_code", None)
            if cabinet_task and not cabinet_task.done():
                cabinet_task.cancel()
            if live_enabled and stock_code:
                cabinet_task = asyncio.create_task(run_cabinet_task(stock_code))
                restarted = True
        await manager.broadcast({"type": "system", "data": {"msg": "配置已更新并生效"}})
        return {"status": "success", "msg": "config saved", "live_restarted": restarted, "live_enabled": live_enabled}
    except Exception as e:
        logger.error(f"/api/config/save failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}

@app.get("/api/report/latest")
async def api_latest_report():
    try:
        load_report_history()
        ranking = []
        summary = None
        strategy_reports = {}
        first = {}
        if report_history and isinstance(report_history, list):
            first = report_history[0] if report_history else {}
            if isinstance(first, dict):
                summary = first.get("summary")
                strategy_reports = first.get("strategy_reports") or {}
        if (not isinstance(summary, dict)) and isinstance(latest_backtest_result, dict):
            summary = latest_backtest_result
            strategy_reports = latest_strategy_reports or {}
        if not isinstance(summary, dict):
            summary = None
        if summary:
            ranking = summary.get("ranking", [])
        if not isinstance(strategy_reports, dict):
            strategy_reports = {}
        reports = [v for v in strategy_reports.values() if isinstance(v, dict)]
        reports = sorted(reports, key=lambda x: str(x.get("strategy_id", "")))
        payload = {
            "report_id": first.get("report_id") if isinstance(first, dict) else None,
            "status": first.get("status") if isinstance(first, dict) else None,
            "error_msg": first.get("error_msg") if isinstance(first, dict) else None,
            "summary": summary,
            "ranking": ranking if isinstance(ranking, list) else [],
            "strategy_reports": reports
        }
        payload = _sanitize_non_finite(payload)
        safe_payload = _safe_json_obj(payload)
        if isinstance(safe_payload, dict):
            return safe_payload
        return {
            "summary": None,
            "ranking": [],
            "strategy_reports": []
        }
    except Exception as e:
        logger.error(f"/api/report/latest failed: {e}", exc_info=True)
        return {"summary": None, "ranking": [], "strategy_reports": []}

@app.get("/api/report/history")
async def api_report_history():
    try:
        load_report_history()
        items = []
        for r in report_history if isinstance(report_history, list) else []:
            if not isinstance(r, dict):
                continue
            summary = r.get("summary") if isinstance(r.get("summary"), dict) else {}
            items.append({
                "report_id": r.get("report_id"),
                "created_at": r.get("created_at"),
                "finished_at": r.get("finished_at"),
                "status": r.get("status", "success" if r.get("summary") else "failed"),
                "error_msg": r.get("error_msg"),
                "stock_code": r.get("stock_code") or summary.get("stock"),
                "period": summary.get("period"),
                "total_trades": summary.get("total_trades", 0)
            })
        return {"reports": items}
    except Exception as e:
        logger.error(f"/api/report/history failed: {e}", exc_info=True)
        return {"reports": []}

@app.get("/api/report/{report_id}")
async def api_report_detail(report_id: str):
    try:
        rid = str(report_id)
        cached = report_detail_cache.get(rid)
        if isinstance(cached, dict):
            return cached
        load_report_history()
        for r in report_history if isinstance(report_history, list) else []:
            if not isinstance(r, dict):
                continue
            if str(r.get("report_id")) == rid:
                summary = r.get("summary") if isinstance(r.get("summary"), dict) else None
                ranking = summary.get("ranking", []) if summary else []
                strategy_reports = r.get("strategy_reports") if isinstance(r.get("strategy_reports"), dict) else {}
                reports = [v for v in strategy_reports.values() if isinstance(v, dict)]
                reports = sorted(reports, key=lambda x: str(x.get("strategy_id", "")))
                payload = {
                    "report_id": r.get("report_id"),
                    "created_at": r.get("created_at"),
                    "finished_at": r.get("finished_at"),
                    "status": r.get("status", "success" if summary else "failed"),
                    "error_msg": r.get("error_msg"),
                    "request": r.get("request") if isinstance(r.get("request"), dict) else {},
                    "summary": summary,
                    "ranking": ranking,
                    "strategy_reports": reports,
                    "ai_review_text": (
                        (str(r.get("ai_review_text", "") or "") if int(r.get("ai_review_version", 0) or 0) == AI_REVIEW_SCHEMA_VERSION else "")
                        or str(report_ai_review_cache.get(str(report_id), "") or "")
                    ),
                    "ai_review_version": int(r.get("ai_review_version", 0) or 0)
                }
                payload = _sanitize_non_finite(payload)
                safe_payload = _safe_json_obj(payload)
                if isinstance(safe_payload, dict):
                    report_detail_cache[rid] = safe_payload
                    if len(report_detail_cache) > 300:
                        first_key = next(iter(report_detail_cache))
                        if first_key != rid:
                            report_detail_cache.pop(first_key, None)
                    return safe_payload
                return {"summary": None, "ranking": [], "strategy_reports": []}
        raise HTTPException(status_code=404, detail="report not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/api/report/{report_id} failed: {e}", exc_info=True)
        return {"summary": None, "ranking": [], "strategy_reports": []}


def _build_ai_report_review(report_item):
    cfg = ConfigLoader.reload()
    api_key = str(cfg.get("data_provider.llm_api_key", "") or "").strip()
    base_url = str(cfg.get("data_provider.llm_api_url", "") or "").strip()
    model_name = str(cfg.get("data_provider.llm_model", "") or "gpt-4o-mini").strip()
    if not api_key or not base_url:
        return ""
    summary = report_item.get("summary") if isinstance(report_item.get("summary"), dict) else {}
    strategy_reports = report_item.get("strategy_reports") if isinstance(report_item.get("strategy_reports"), dict) else {}
    compact_reports = []

    def _trade_nodes(trades, direction, max_items=8):
        out = []
        for t in trades:
            if not isinstance(t, dict):
                continue
            if str(t.get("direction", "")).upper() != direction:
                continue
            out.append({
                "dt": t.get("dt"),
                "price": t.get("price"),
                "quantity": t.get("quantity"),
                "reason": t.get("reason"),
                "pnl": t.get("pnl")
            })
            if len(out) >= max_items:
                break
        return out

    for sid, rep in strategy_reports.items():
        if not isinstance(rep, dict):
            continue
        trades = rep.get("trade_details") if isinstance(rep.get("trade_details"), list) else []
        compact_reports.append({
            "strategy_id": sid,
            "kline_type": rep.get("kline_type"),
            "period_label": rep.get("period_label"),
            "score_total": rep.get("score_total"),
            "annualized_roi": rep.get("annualized_roi"),
            "max_dd": rep.get("max_dd"),
            "win_rate": rep.get("win_rate"),
            "total_trades": rep.get("total_trades"),
            "force_close_count": rep.get("force_close_count", 0),
            "last_trade_reason": trades[-1].get("reason") if trades else None,
            "buy_nodes": _trade_nodes(trades, "BUY"),
            "sell_nodes": _trade_nodes(trades, "SELL")
        })
    req_payload = {
        "stock_code": report_item.get("stock_code"),
        "request": report_item.get("request"),
        "summary": summary,
        "strategy_reports": compact_reports
    }
    url = base_url.rstrip("/")
    if not url.endswith("/chat/completions"):
        if url.endswith("/v1"):
            url = f"{url}/chat/completions"
        else:
            url = f"{url}/v1/chat/completions"
    system_prompt = "你是A股量化复盘分析师，请根据回测摘要与交易明细给出结构化复盘，必须具体到交易节点与参数值。"
    user_prompt = (
        "请输出简洁Markdown，必须严格包含六段：\n"
        "1) 核心结论\n"
        "2) 关键问题\n"
        "3) 基于交易明细的买入节点分析（逐条说明买入核心依据、信号逻辑、触发原因）\n"
        "4) 基于交易明细的卖出节点分析（逐条说明卖出核心依据、信号逻辑、触发原因）\n"
        "5) 参数优化建议（必须给出明确参数值，不要只给方向）\n"
        "6) 下一轮实验方案（A/B至少两组，直接列出参数值对比）\n\n"
        "注意：如果某段缺少交易节点，请写“本周期无该类交易节点”。\n"
        f"回测数据：\n{json.dumps(req_payload, ensure_ascii=False)}"
    )
    payload = {
        "model": model_name,
        "temperature": 0.2,
        "max_tokens": 1000,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt}
        ]
    }
    try:
        req = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            method="POST"
        )
        with urllib.request.urlopen(req, timeout=45) as resp:
            raw = resp.read().decode("utf-8")
        obj = json.loads(raw)
        return str(obj.get("choices", [{}])[0].get("message", {}).get("content", "")).strip()
    except Exception:
        return ""


@app.post("/api/report/{report_id}/ai_review")
async def api_report_ai_review(report_id: str):
    try:
        load_report_history()
        for idx, r in enumerate(report_history if isinstance(report_history, list) else []):
            if not isinstance(r, dict):
                continue
            if str(r.get("report_id")) != str(report_id):
                continue
            rid = str(report_id)
            cached = str(report_ai_review_cache.get(rid, "") or "").strip()
            cached_ver = int(report_ai_review_cache.get(f"{rid}__v", 0) or 0)
            if cached and cached_ver == AI_REVIEW_SCHEMA_VERSION:
                return {"status": "success", "report_id": report_id, "analysis": cached, "cached": True}
            cached = str(r.get("ai_review_text", "") or "").strip()
            persisted_ver = int(r.get("ai_review_version", 0) or 0)
            if cached and persisted_ver == AI_REVIEW_SCHEMA_VERSION:
                report_ai_review_cache[rid] = cached
                report_ai_review_cache[f"{rid}__v"] = AI_REVIEW_SCHEMA_VERSION
                return {"status": "success", "report_id": report_id, "analysis": cached, "cached": True}
            analysis = _build_ai_report_review(r)
            if not analysis:
                return {"status": "error", "msg": "AI复盘生成失败，请检查模型配置"}
            report_history[idx]["ai_review_text"] = analysis
            report_history[idx]["ai_review_version"] = AI_REVIEW_SCHEMA_VERSION
            report_ai_review_cache[rid] = analysis
            report_ai_review_cache[f"{rid}__v"] = AI_REVIEW_SCHEMA_VERSION
            persist_report_history()
            return {"status": "success", "report_id": report_id, "analysis": analysis, "cached": False}
        return {"status": "error", "msg": "report not found"}
    except Exception as e:
        logger.error(f"/api/report/{report_id}/ai_review failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.get("/api/report/strategy/kline_data")
async def api_report_strategy_kline_data(report_id: str, strategy_id: str):
    try:
        cache_key = f"{str(report_id)}|{str(strategy_id)}"
        cached_payload = report_strategy_kline_cache.get(cache_key)
        if isinstance(cached_payload, dict):
            return cached_payload
        load_report_history()
        target_report = None
        for r in report_history if isinstance(report_history, list) else []:
            if isinstance(r, dict) and str(r.get("report_id")) == str(report_id):
                target_report = r
                break
        if not isinstance(target_report, dict):
            return {"status": "error", "msg": "report not found"}
        strategy_reports = target_report.get("strategy_reports") if isinstance(target_report.get("strategy_reports"), dict) else {}
        srep = strategy_reports.get(str(strategy_id))
        if not isinstance(srep, dict):
            return {"status": "error", "msg": "strategy report not found"}
        summary = target_report.get("summary") if isinstance(target_report.get("summary"), dict) else {}
        stock_code = _normalize_symbol(target_report.get("stock_code") or summary.get("stock") or "")
        if not stock_code:
            return {"status": "error", "msg": "missing stock code"}
        start_text = str(srep.get("start_date") or "").strip()
        end_text = str(srep.get("end_date") or "").strip()
        if not start_text or not end_text:
            return {"status": "error", "msg": "missing strategy period"}
        start_dt = pd.to_datetime(start_text)
        end_dt = pd.to_datetime(end_text)
        if pd.isna(start_dt) or pd.isna(end_dt):
            return {"status": "error", "msg": "invalid strategy period"}
        period_label = _strategy_period_label(strategy_id, srep=srep)
        interval = _period_label_to_interval(period_label)
        provider = _select_provider()
        df = provider.fetch_kline_data(stock_code, start_dt, end_dt, interval=interval) if hasattr(provider, "fetch_kline_data") else pd.DataFrame()
        if df is None or df.empty:
            return {"status": "error", "msg": "no kline data"}
        if "dt" not in df.columns:
            return {"status": "error", "msg": "missing dt"}
        if "vol" not in df.columns and "volume" in df.columns:
            df["vol"] = df["volume"]
        if "volume" not in df.columns and "vol" in df.columns:
            df["volume"] = df["vol"]
        for c in ["open", "high", "low", "close", "volume"]:
            if c not in df.columns:
                return {"status": "error", "msg": f"missing {c}"}
        df["dt"] = pd.to_datetime(df["dt"])
        df = df.dropna(subset=["dt"]).sort_values("dt")
        candles = []
        volumes = []
        candle_keys = set()
        for _, row in df.iterrows():
            ts = int(pd.Timestamp(row["dt"]).timestamp())
            candle_keys.add(ts)
            o = float(row["open"])
            c = float(row["close"])
            candles.append({
                "time": ts,
                "open": o,
                "high": float(row["high"]),
                "low": float(row["low"]),
                "close": c
            })
            volumes.append({
                "time": ts,
                "value": float(row["volume"]),
                "color": "#ef4444" if c >= o else "#22c55e"
            })
        trade_rows = srep.get("trade_details") if isinstance(srep.get("trade_details"), list) else []
        markers = []
        for t in trade_rows:
            if not isinstance(t, dict):
                continue
            dt = pd.to_datetime(t.get("dt"))
            if pd.isna(dt):
                continue
            marker_ts = int(pd.Timestamp(dt).timestamp())
            if interval == "D":
                marker_ts = int(pd.Timestamp(dt.date()).timestamp())
            direction = str(t.get("direction", "")).upper()
            is_buy = direction == "BUY"
            price_val = float(t.get("price", 0) or 0)
            markers.append({
                "time": marker_ts,
                "position": "belowBar" if is_buy else "aboveBar",
                "shape": "arrowUp" if is_buy else "arrowDown",
                "color": "#a855f7" if is_buy else "#06b6d4",
                "text": f"{'买' if is_buy else '卖'} {price_val:.2f}"
            })
        payload = {
            "status": "success",
            "stock": stock_code,
            "interval": interval,
            "period_label": period_label,
            "strategy_id": str(strategy_id),
            "candles": candles,
            "volumes": volumes,
            "markers": markers
        }
        report_strategy_kline_cache[cache_key] = payload
        if len(report_strategy_kline_cache) > 300:
            first_key = next(iter(report_strategy_kline_cache))
            if first_key != cache_key:
                report_strategy_kline_cache.pop(first_key, None)
        return payload
    except Exception as e:
        logger.error(f"/api/report/strategy_kline_data failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.post("/api/report/delete")
async def api_report_delete(req: ReportDeleteRequest):
    global report_history, latest_backtest_result, latest_strategy_reports, report_strategy_kline_cache, report_ai_review_cache, report_detail_cache
    rid = str(req.report_id or "").strip()
    if not rid:
        return {"status": "error", "msg": "report_id is required"}
    try:
        load_report_history()
        before = len(report_history) if isinstance(report_history, list) else 0
        report_history = [r for r in report_history if str(r.get("report_id")) != rid] if isinstance(report_history, list) else []
        deleted = len(report_history) != before
        if deleted:
            report_ai_review_cache.pop(rid, None)
            report_ai_review_cache.pop(f"{rid}__v", None)
            report_detail_cache.pop(rid, None)
            report_strategy_kline_cache = {k: v for k, v in report_strategy_kline_cache.items() if not str(k).startswith(f"{rid}|")}
            persist_report_history()
            _rebuild_strategy_score_cache()
            latest_backtest_result = None
            latest_strategy_reports = {}
            if report_history and isinstance(report_history[0], dict):
                latest_backtest_result = report_history[0].get("summary")
                latest_strategy_reports = report_history[0].get("strategy_reports") if isinstance(report_history[0].get("strategy_reports"), dict) else {}
            return {"status": "success", "deleted": True}
        return {"status": "info", "deleted": False}
    except Exception as e:
        logger.error(f"/api/report/delete failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


def _select_provider():
    cfg = ConfigLoader.reload()
    provider_source = current_provider_source or cfg.get("data_provider.source", "default")
    if provider_source == "tushare":
        return TushareProvider(token=cfg.get("data_provider.tushare_token"))
    if provider_source == "akshare":
        return AkshareProvider()
    return DataProvider()


def _normalize_symbol(code):
    c = str(code or "").strip().upper()
    if c.endswith(".SH") or c.endswith(".SZ"):
        return c
    if len(c) == 6 and c.isdigit():
        return f"{c}.SH" if c.startswith("6") else f"{c}.SZ"
    return c


def _period_label_to_interval(period_label):
    p = str(period_label or "").strip()
    if p in {"1分钟", "1min"}:
        return "1min"
    if p in {"5分钟", "5min"}:
        return "5min"
    if p in {"10分钟", "10min"}:
        return "10min"
    if p in {"15分钟", "15min"}:
        return "15min"
    if p in {"30分钟", "30min"}:
        return "30min"
    if p in {"60分钟", "60min", "1小时"}:
        return "60min"
    return "D"


def _kline_type_to_period_label(kline_type):
    tf = str(kline_type or "").strip()
    low = tf.lower()
    if low in {"d", "1d", "day", "daily"}:
        return "日线"
    if low.endswith("min"):
        return f"{low.replace('min', '')}分钟"
    return tf or "1分钟"


def _strategy_period_label(strategy_id, srep=None):
    if isinstance(srep, dict):
        tf = str(srep.get("kline_type", "")).strip()
        if tf:
            return _kline_type_to_period_label(tf)
        pl = str(srep.get("period_label", "")).strip()
        if pl:
            return pl
    sid = str(strategy_id or "")
    try:
        for item in list_all_strategy_meta():
            if str(item.get("id", "")) == sid:
                return _kline_type_to_period_label(item.get("kline_type", "1min"))
    except Exception:
        pass
    return "1分钟"


def _cache_key_daily(stock_code, start_dt, end_dt):
    return f"{stock_code}|{start_dt.strftime('%Y-%m-%d')}|{end_dt.strftime('%Y-%m-%d')}"


def _get_cached_daily_df(stock_code, start_dt, end_dt):
    key = _cache_key_daily(stock_code, start_dt, end_dt)
    cached = kline_daily_cache.get(key)
    if isinstance(cached, pd.DataFrame) and not cached.empty:
        return cached.copy()
    provider = _select_provider()
    df = pd.DataFrame()
    if hasattr(provider, "fetch_kline_data"):
        df = provider.fetch_kline_data(stock_code, start_dt, end_dt, interval="D")
    if (df is None or df.empty) and hasattr(provider, "fetch_minute_data"):
        mdf = provider.fetch_minute_data(stock_code, start_dt, end_dt)
        if mdf is not None and not mdf.empty:
            from src.utils.indicators import Indicators
            df = Indicators.resample(mdf, "D")
    if df is None or df.empty:
        return pd.DataFrame()
    kline_daily_cache[key] = df.copy()
    if len(kline_daily_cache) > 20:
        first_key = next(iter(kline_daily_cache))
        if first_key != key:
            kline_daily_cache.pop(first_key, None)
    return df.copy()


def _build_backtest_kline_payload(stock_code, start_dt, end_dt):
    df = _get_cached_daily_df(stock_code, start_dt, end_dt)
    if df is None or df.empty:
        return None
    if "dt" not in df.columns:
        raise RuntimeError("missing dt")
    if "vol" not in df.columns and "volume" in df.columns:
        df["vol"] = df["volume"]
    if "volume" not in df.columns and "vol" in df.columns:
        df["volume"] = df["vol"]
    for c in ["open", "high", "low", "close", "volume"]:
        if c not in df.columns:
            raise RuntimeError(f"missing {c}")
    df["dt"] = pd.to_datetime(df["dt"])
    df = df.dropna(subset=["dt"]).sort_values("dt")
    progress_date = None
    progress_date_text = None
    if current_backtest_report:
        raw_current_date = current_backtest_progress.get("current_date")
        text_current_date = str(raw_current_date or "").strip()
        if text_current_date.lower() == "done":
            current_date = pd.to_datetime(end_dt, errors="coerce")
        elif text_current_date.lower() == "failed":
            current_date = pd.NaT
        else:
            current_date = pd.to_datetime(raw_current_date, errors="coerce")
        if not pd.isna(current_date):
            progress_date = current_date
            progress_date_text = current_date.strftime("%Y-%m-%d")
    df = df[(df["dt"] >= start_dt) & (df["dt"] <= end_dt)]
    if df.empty:
        return {"candles": [], "volumes": [], "markers": [], "strategies": [], "progress_date": progress_date_text}
    plot_df = df[["dt", "open", "high", "low", "close", "volume"]].copy()
    plot_df["dt"] = pd.to_datetime(plot_df["dt"])
    candles = []
    volumes = []
    for _, r in plot_df.iterrows():
        t = r["dt"].strftime("%Y-%m-%d")
        o = float(r["open"])
        h = float(r["high"])
        l = float(r["low"])
        c = float(r["close"])
        v = float(r["volume"])
        candles.append({"time": t, "open": o, "high": h, "low": l, "close": c})
        volumes.append({"time": t, "value": v, "color": "#ef4444" if c >= o else "#22c55e"})
    symbol_plain = stock_code.replace(".SH", "").replace(".SZ", "")
    trades = [
        t for t in current_backtest_trades
        if str(t.get("code", "")).replace(".SH", "").replace(".SZ", "") == symbol_plain
    ]
    strategy_ids = sorted(set(str(t.get("strategy", "")).strip() for t in trades if str(t.get("strategy", "")).strip()))
    palette = [
        "#60a5fa", "#a78bfa", "#22d3ee", "#f59e0b", "#f472b6", "#38bdf8", "#c084fc", "#fb7185",
        "#2dd4bf", "#fbbf24", "#818cf8", "#06b6d4", "#e879f9", "#0ea5e9", "#f97316", "#8b5cf6",
        "#14b8a6", "#93c5fd", "#f0abfc", "#67e8f9", "#fcd34d", "#7dd3fc", "#d8b4fe", "#f9a8d4"
    ]
    color_map = {sid: palette[i % len(palette)] for i, sid in enumerate(strategy_ids)}
    strategy_name_map = {str(x.get("id", "")): str(x.get("name", "")) for x in list_all_strategy_meta()}
    markers = []
    for t in trades:
        sid = str(t.get("strategy", "")).strip()
        if not sid:
            continue
        dt = pd.to_datetime(t.get("dt"))
        if pd.isna(dt):
            continue
        d = dt.strftime("%Y-%m-%d")
        if d < start_dt.strftime("%Y-%m-%d") or d > end_dt.strftime("%Y-%m-%d"):
            continue
        if progress_date is not None and dt.date() > progress_date.date():
            continue
        direction = str(t.get("dir", "")).upper()
        is_buy = direction == "BUY"
        trade_price = t.get("price")
        try:
            price_text = f"{float(trade_price):.2f}"
        except Exception:
            price_text = ""
        markers.append({
            "time": d,
            "strategy_id": sid,
            "position": "belowBar" if is_buy else "aboveBar",
            "shape": "arrowUp" if is_buy else "arrowDown",
            "color": color_map.get(sid, "#60a5fa"),
            "text": price_text
        })
    strategy_legends = [{"id": sid, "name": strategy_name_map.get(sid, f"策略{sid}"), "color": color_map[sid]} for sid in strategy_ids]
    return {
        "candles": candles,
        "volumes": volumes,
        "markers": markers,
        "strategies": strategy_legends,
        "progress_date": progress_date_text
    }


def _pattern_thumb_path(stock_code, start_dt, end_dt):
    os.makedirs(PATTERN_THUMB_DIR, exist_ok=True)
    norm = _normalize_symbol(stock_code).replace(".", "_")
    s = pd.to_datetime(start_dt).strftime("%Y%m%d")
    e = pd.to_datetime(end_dt).strftime("%Y%m%d")
    return os.path.join(PATTERN_THUMB_DIR, f"{norm}_{s}_{e}.png")


def _render_pattern_thumb_png(stock_code, start_dt, end_dt):
    import mplfinance as mpf
    import matplotlib.pyplot as plt
    img_path = _pattern_thumb_path(stock_code, start_dt, end_dt)
    if os.path.exists(img_path):
        return img_path
    df = _get_cached_daily_df(stock_code, start_dt, end_dt)
    if df is None or df.empty:
        return None
    if "dt" not in df.columns:
        return None
    for c in ["open", "high", "low", "close"]:
        if c not in df.columns:
            return None
    plot_df = df[["dt", "open", "high", "low", "close"]].copy()
    plot_df["Date"] = pd.to_datetime(plot_df["dt"])
    plot_df = plot_df.set_index("Date")
    plot_df = plot_df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
    mc = mpf.make_marketcolors(up="#ef4444", down="#22c55e", edge="inherit", wick="inherit", volume="inherit")
    s = mpf.make_mpf_style(base_mpf_style="charles", marketcolors=mc, facecolor="#020617", edgecolor="#334155", figcolor="#020617", gridcolor="#334155")
    fig, _ = mpf.plot(
        plot_df,
        type="candle",
        style=s,
        volume=False,
        title=f"{stock_code} 日K",
        returnfig=True,
        figsize=(4.4, 2.1),
        xrotation=0
    )
    fig.savefig(img_path, format="png", dpi=130, bbox_inches="tight", pad_inches=0.05)
    plt.close(fig)
    return img_path


def _warmup_classic_pattern_thumbs():
    os.makedirs(PATTERN_THUMB_DIR, exist_ok=True)
    ok = 0
    for item in CLASSIC_PATTERN_ITEMS:
        try:
            stock_code = _normalize_symbol(item["stock"])
            start_dt = pd.to_datetime(item["start"])
            end_dt = pd.to_datetime(item["end"])
            if pd.isna(start_dt) or pd.isna(end_dt) or start_dt > end_dt:
                continue
            p = _render_pattern_thumb_png(stock_code, start_dt, end_dt)
            if p and os.path.exists(p):
                ok += 1
        except Exception:
            continue
    logger.info(f"classic pattern thumbs ready: {ok}/{len(CLASSIC_PATTERN_ITEMS)}")


@app.get("/api/backtest/kline_data")
async def api_backtest_kline_data(stock: str, start: str, end: str):
    try:
        stock_code = _normalize_symbol(stock)
        start_dt = pd.to_datetime(start)
        end_dt = pd.to_datetime(end)
        if pd.isna(start_dt) or pd.isna(end_dt) or start_dt > end_dt:
            return {"status": "error", "msg": "invalid date range"}
        payload = _build_backtest_kline_payload(stock_code, start_dt, end_dt)
        if payload is None:
            return {"status": "error", "msg": "no data"}
        return {"status": "success", "stock": stock_code, **payload}
    except Exception as e:
        logger.error(f"/api/backtest/kline_data failed: {e}", exc_info=True)
        return {"status": "error", "msg": str(e)}


@app.get("/api/backtest/kline_chart")
async def api_backtest_kline_chart(stock: str, start: str, end: str):
    try:
        import mplfinance as mpf
        import matplotlib.pyplot as plt
        stock_code = _normalize_symbol(stock)
        start_dt = pd.to_datetime(start)
        end_dt = pd.to_datetime(end)
        if pd.isna(start_dt) or pd.isna(end_dt) or start_dt > end_dt:
            return Response(content="invalid date range", media_type="text/plain", status_code=400)
        payload = _build_backtest_kline_payload(stock_code, start_dt, end_dt)
        if payload is None:
            return Response(content="no data", media_type="text/plain", status_code=404)
        if not payload["candles"]:
            return Response(content="no visible bars", media_type="text/plain", status_code=404)
        plot_df = pd.DataFrame(payload["candles"]).copy()
        plot_df["Date"] = pd.to_datetime(plot_df["time"])
        plot_df = plot_df.set_index("Date")
        plot_df = plot_df.rename(columns={"open": "Open", "high": "High", "low": "Low", "close": "Close"})
        vol_df = pd.DataFrame(payload["volumes"]).copy()
        vol_df["Date"] = pd.to_datetime(vol_df["time"])
        vol_df = vol_df.set_index("Date")
        plot_df["Volume"] = vol_df["value"]
        strategy_ids = [x["id"] for x in payload["strategies"]]
        color_map = {x["id"]: x["color"] for x in payload["strategies"]}
        date_index_map = {d.strftime("%Y-%m-%d"): d for d in plot_df.index}
        buy_map = {sid: pd.Series(np.nan, index=plot_df.index) for sid in strategy_ids}
        sell_map = {sid: pd.Series(np.nan, index=plot_df.index) for sid in strategy_ids}
        for m in payload["markers"]:
            sid = str(m.get("strategy_id", ""))
            if sid not in buy_map:
                continue
            t = str(m.get("time", ""))
            candle_dt = date_index_map.get(t)
            if candle_dt is None:
                continue
            if m.get("shape") == "arrowUp":
                buy_map[sid].loc[candle_dt] = float(plot_df.loc[candle_dt, "Low"]) * 0.995
            else:
                sell_map[sid].loc[candle_dt] = float(plot_df.loc[candle_dt, "High"]) * 1.005
        addplots = []
        legend_handles = []
        for st in payload["strategies"]:
            sid = st["id"]
            color = color_map[sid]
            if buy_map[sid].notna().any():
                addplots.append(mpf.make_addplot(buy_map[sid], type="scatter", marker="^", markersize=60, color=color, panel=0))
            if sell_map[sid].notna().any():
                addplots.append(mpf.make_addplot(sell_map[sid], type="scatter", marker="v", markersize=60, color=color, panel=0))
            legend_handles.append(mlines.Line2D([], [], color=color, marker="o", linestyle="None", label=st["name"]))
        legend_handles.append(mlines.Line2D([], [], color="#e2e8f0", marker="^", linestyle="None", label="买入信号"))
        legend_handles.append(mlines.Line2D([], [], color="#e2e8f0", marker="v", linestyle="None", label="卖出信号"))
        plot_kwargs = {
            "type": "candle",
            "style": "charles",
            "volume": True,
            "title": f"{stock_code} 日K线（含成交量）",
            "returnfig": True,
            "figsize": (13, 8)
        }
        if addplots:
            plot_kwargs["addplot"] = addplots
        fig, axes = mpf.plot(plot_df, **plot_kwargs)
        if axes and legend_handles:
            axes[0].legend(handles=legend_handles, loc="upper left", fontsize=8, ncol=2, framealpha=0.65)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", dpi=120, bbox_inches="tight")
        plt.close(fig)
        buf.seek(0)
        return StreamingResponse(buf, media_type="image/png")
    except Exception as e:
        logger.error(f"/api/backtest/kline_chart failed: {e}", exc_info=True)
        return Response(content=str(e), media_type="text/plain", status_code=500)


@app.get("/api/backtest/kline_thumb")
async def api_backtest_kline_thumb(stock: str, start: str, end: str):
    try:
        stock_code = _normalize_symbol(stock)
        start_dt = pd.to_datetime(start)
        end_dt = pd.to_datetime(end)
        if pd.isna(start_dt) or pd.isna(end_dt) or start_dt > end_dt:
            return Response(content="invalid date range", media_type="text/plain", status_code=400)
        img_path = _render_pattern_thumb_png(stock_code, start_dt, end_dt)
        if not img_path or not os.path.exists(img_path):
            return Response(content="no data", media_type="text/plain", status_code=404)
        return FileResponse(img_path, media_type="image/png")
    except Exception as e:
        logger.error(f"/api/backtest/kline_thumb failed: {e}", exc_info=True)
        return Response(content=str(e), media_type="text/plain", status_code=500)

# --- Control Endpoints for External Systems (e.g. OpenClaw) ---
@app.post("/api/control/start_backtest")
async def api_start_backtest(req: BacktestRequest):
    """Start a backtest task (useful for OpenClaw API calls)"""
    global cabinet_task
    logger.info(
        "start_backtest request params: stock_code=%s strategy_id=%s strategy_ids=%s strategy_mode=%s start=%s end=%s capital=%s",
        req.stock_code,
        req.strategy_id,
        req.strategy_ids,
        req.strategy_mode,
        req.start,
        req.end,
        req.capital,
    )
    if cabinet_task and not cabinet_task.done():
        cabinet_task.cancel()
    report_id = start_new_backtest_report(req.stock_code, req.strategy_id, {
        "stock_code": req.stock_code,
        "strategy_id": req.strategy_id,
        "strategy_ids": req.strategy_ids,
        "strategy_mode": req.strategy_mode,
        "start": req.start,
        "end": req.end,
        "capital": req.capital
    })
    cabinet_task = asyncio.create_task(run_backtest_task(req.stock_code, req.strategy_id, req.strategy_mode, req.start, req.end, req.capital, req.strategy_ids))
    return {"status": "success", "msg": f"Backtest started for {req.stock_code}", "report_id": report_id}

@app.post("/api/control/start_live")
async def api_start_live(req: LiveRequest):
    """Start a live simulation task"""
    if not is_live_enabled():
        return {"status": "error", "msg": "Live功能已在配置中关闭（system.enable_live=false）"}
    global cabinet_task
    if cabinet_task and not cabinet_task.done():
        cabinet_task.cancel()
    cabinet_task = asyncio.create_task(run_cabinet_task(req.stock_code))
    return {"status": "success", "msg": f"Live monitoring started for {req.stock_code}"}

@app.post("/api/control/stop")
async def api_stop_task():
    """Stop the current running task"""
    global cabinet_task
    if cabinet_task and not cabinet_task.done():
        cabinet_task.cancel()
        if current_backtest_report and str(current_backtest_report.get("status", "")).lower() == "running":
            cancel_current_backtest_report("backtest task cancelled by user")
            await manager.broadcast({"type": "system", "data": {"msg": "回测已手动终止"}})
            return {"status": "success", "msg": "Backtest stopped"}
        await manager.broadcast({"type": "system", "data": {"msg": "内阁监控已手动停止"}})
        return {"status": "success", "msg": "Task stopped"}
    return {"status": "info", "msg": "No task is currently running"}

@app.post("/api/control/switch_strategy")
async def api_switch_strategy(req: StrategySwitchRequest):
    """Switch the active strategy on the fly"""
    global current_cabinet
    selected = req.strategy_ids if req.strategy_ids else req.strategy_id
    if current_cabinet:
        current_cabinet.set_active_strategies(selected if selected else 'all')
        return {"status": "success", "msg": f"Strategy switched to {selected}"}
    return {"status": "error", "msg": "No active cabinet running"}

@app.post("/api/control/set_source")
async def api_set_source(req: SourceSwitchRequest):
    global cabinet_task, current_provider_source, current_cabinet
    source = str(req.source or "").lower().strip()
    if source not in {"default", "tushare", "akshare"}:
        return {"status": "error", "msg": "source must be one of: default, tushare, akshare"}
    config.set("data_provider.source", source)
    current_provider_source = source
    restarted = False
    stock_code = None
    if current_cabinet and type(current_cabinet).__name__ == "LiveCabinet":
        stock_code = getattr(current_cabinet, "stock_code", None)
    if stock_code:
        if cabinet_task and not cabinet_task.done():
            cabinet_task.cancel()
        cabinet_task = asyncio.create_task(run_cabinet_task(stock_code))
        restarted = True
    await manager.broadcast({"type": "system", "data": {"msg": f"数据源已切换为 {source}"}})
    return {"status": "success", "msg": f"source switched to {source}", "source": source, "live_restarted": restarted}

@app.post("/api/control/reload_strategies")
async def api_reload_strategies():
    """Hot reload strategies without restarting the server"""
    logger.info("Received request to reload strategies...")
    try:
        # Reload the implemented_strategies module first
        if 'src.strategies.implemented_strategies' in sys.modules:
            importlib.reload(sys.modules['src.strategies.implemented_strategies'])
            logger.info("Reloaded module: src.strategies.implemented_strategies")
        
        # Then reload the strategy_factory module
        importlib.reload(strategy_factory_module)
        logger.info("Reloaded module: src.strategies.strategy_factory")
        
        # Test if we can create strategies
        strategies = strategy_factory_module.create_strategies()
        strategy_count = len(strategies)
        
        strategy_names = [s.name for s in strategies]
        logger.info(f"Strategy Factory Reloaded. Current Strategies ({strategy_count}): {strategy_names}")
        
        return {
            "status": "success", 
            "msg": f"Successfully reloaded {strategy_count} strategies.",
            "strategies": strategy_names
        }
    except Exception as e:
        logger.error(f"Failed to reload strategies: {str(e)}", exc_info=True)
        return {"status": "error", "msg": f"Failed to reload strategies: {str(e)}"}

@app.get("/api/status")
async def api_get_status():
    """Get current system status"""
    is_running = cabinet_task is not None and not cabinet_task.done()
    return {
        "is_running": is_running,
        "active_cabinet_type": type(current_cabinet).__name__ if current_cabinet else None,
        "provider_source": current_provider_source or config.get("data_provider.source", "default"),
        "live_enabled": is_live_enabled(),
        "progress": current_backtest_progress,
        "current_report_id": current_backtest_report.get("report_id") if current_backtest_report else None,
        "current_report_status": current_backtest_report.get("status") if current_backtest_report else None,
        "current_report_error": current_backtest_report.get("error_msg") if current_backtest_report else None
    }

def _history_sync_payload_from_request(req: HistorySyncRunRequest):
    return {
        "codes": req.codes,
        "tables": req.tables,
        "start_time": req.start_time,
        "end_time": req.end_time,
        "lookback_days": max(1, int(req.lookback_days or 1)),
        "max_codes": max(1, int(req.max_codes or 1)),
        "batch_size": max(1, int(req.batch_size or 1)),
        "dry_run": bool(req.dry_run),
        "on_duplicate": str(req.on_duplicate or "ignore"),
    }

async def _run_history_sync_once(payload: dict):
    result = await asyncio.to_thread(history_sync_service.run_sync, payload)
    logger.info(f"history sync finished: {result.get('status')}")
    return result

async def _history_sync_scheduler_loop():
    while True:
        cfg = ConfigLoader.reload()
        interval = max(1, int(cfg.get("history_sync.interval_minutes", 60) or 60))
        payload = {
            "codes": cfg.get("history_sync.codes", None),
            "tables": cfg.get("history_sync.tables", list(TABLE_INTERVAL_MAP.keys())),
            "start_time": cfg.get("history_sync.start_time", None),
            "end_time": cfg.get("history_sync.end_time", None),
            "lookback_days": max(1, int(cfg.get("history_sync.lookback_days", 10) or 10)),
            "max_codes": max(1, int(cfg.get("history_sync.max_codes", 200) or 200)),
            "batch_size": max(1, int(cfg.get("history_sync.batch_size", 500) or 500)),
            "dry_run": bool(cfg.get("history_sync.dry_run", False)),
            "on_duplicate": str(cfg.get("history_sync.on_duplicate", "ignore") or "ignore"),
        }
        try:
            await _run_history_sync_once(payload)
        except Exception as e:
            logger.error(f"history sync scheduler failed: {e}", exc_info=True)
        await asyncio.sleep(interval * 60)

@app.post("/api/history_sync/run")
async def api_history_sync_run(req: HistorySyncRunRequest):
    payload = _history_sync_payload_from_request(req)
    if req.async_run:
        asyncio.create_task(_run_history_sync_once(payload))
        return {"status": "accepted", "msg": "history sync task started", "payload": payload}
    return await _run_history_sync_once(payload)

@app.get("/api/history_sync/status")
async def api_history_sync_status():
    return {
        "status": "success",
        "service": history_sync_service.get_status(),
        "scheduler_running": history_sync_scheduler_task is not None and not history_sync_scheduler_task.done(),
    }

@app.post("/api/history_sync/scheduler/start")
async def api_history_sync_scheduler_start(req: HistorySyncScheduleRequest):
    global history_sync_scheduler_task
    cfg = ConfigLoader.reload()
    cfg.set("history_sync.scheduler_enabled", True)
    cfg.set("history_sync.interval_minutes", max(1, int(req.interval_minutes or 1)))
    cfg.set("history_sync.lookback_days", max(1, int(req.lookback_days or 1)))
    cfg.set("history_sync.max_codes", max(1, int(req.max_codes or 1)))
    cfg.set("history_sync.batch_size", max(1, int(req.batch_size or 1)))
    cfg.set("history_sync.tables", req.tables if req.tables else list(TABLE_INTERVAL_MAP.keys()))
    cfg.set("history_sync.dry_run", bool(req.dry_run))
    cfg.set("history_sync.on_duplicate", str(req.on_duplicate or "ignore"))
    cfg.save()
    if history_sync_scheduler_task is None or history_sync_scheduler_task.done():
        history_sync_scheduler_task = asyncio.create_task(_history_sync_scheduler_loop())
    return {
        "status": "success",
        "msg": "history sync scheduler started",
        "scheduler_running": True,
    }

@app.post("/api/history_sync/scheduler/stop")
async def api_history_sync_scheduler_stop():
    global history_sync_scheduler_task
    cfg = ConfigLoader.reload()
    cfg.set("history_sync.scheduler_enabled", False)
    cfg.save()
    if history_sync_scheduler_task and not history_sync_scheduler_task.done():
        history_sync_scheduler_task.cancel()
    return {"status": "success", "msg": "history sync scheduler stopped"}


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []
        self.connection_queues = {}
        self.sender_tasks = {}
        self.queue_maxsize = 20000
        self.send_timeout_sec = 5.0

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)
        q = asyncio.Queue(maxsize=self.queue_maxsize)
        self.connection_queues[websocket] = q
        self.sender_tasks[websocket] = asyncio.create_task(self._sender_loop(websocket, q))

    def disconnect(self, websocket: WebSocket):
        if websocket in self.active_connections:
            self.active_connections.remove(websocket)
        task = self.sender_tasks.pop(websocket, None)
        if task is not None:
            task.cancel()
        self.connection_queues.pop(websocket, None)

    async def _sender_loop(self, websocket: WebSocket, q: asyncio.Queue):
        try:
            while True:
                payload = await q.get()
                try:
                    await asyncio.wait_for(websocket.send_json(payload), timeout=self.send_timeout_sec)
                except Exception as e:
                    print(f"WS Error: {e}")
                    break
        except asyncio.CancelledError:
            pass
        finally:
            self.disconnect(websocket)

    async def broadcast(self, message: dict):
        for connection in list(self.active_connections):
            q = self.connection_queues.get(connection)
            if q is None:
                continue
            try:
                q.put_nowait(message)
            except asyncio.QueueFull:
                try:
                    _ = q.get_nowait()
                    q.put_nowait(message)
                except Exception:
                    self.disconnect(connection)

manager = ConnectionManager()

@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    
    # Do NOT send strategies immediately. Wait for start_simulation command.
    
    try:
        while True:
            data = await websocket.receive_text()
            # Handle commands
            try:
                cmd = json.loads(data)
                print(f"Received command: {cmd}")
                
                if cmd.get("type") == "reload_strategies":
                    # Reload the modules dynamically via websocket command
                    try:
                        if 'src.strategies.implemented_strategies' in sys.modules:
                            importlib.reload(sys.modules['src.strategies.implemented_strategies'])
                        importlib.reload(strategy_factory_module)
                        strategies = strategy_factory_module.create_strategies()
                        await manager.broadcast({"type": "system", "data": {"msg": f"策略热更新成功，当前共 {len(strategies)} 个策略"}})
                    except Exception as e:
                        await manager.broadcast({"type": "system", "data": {"msg": f"策略热更新失败: {str(e)}"}})

                elif cmd.get("type") == "start_simulation":
                    if not is_live_enabled():
                        await manager.broadcast({"type": "system", "data": {"msg": "Live功能已在配置中关闭（system.enable_live=false）"}})
                        continue
                    stock_code = cmd.get("stock", "600036.SH")
                    # Start async task
                    # Check if already running?
                    # The wrapper run_cabinet_task handles new instance creation.
                    # But we need to track the task to cancel it later.
                    global cabinet_task
                    if cabinet_task and not cabinet_task.done():
                        cabinet_task.cancel()
                        
                    cabinet_task = asyncio.create_task(run_cabinet_task(stock_code))
                
                elif cmd.get("type") == "start_backtest":
                    stock_code = cmd.get("stock", "600036.SH")
                    strategy_id = cmd.get("strategy", "all")
                    strategy_ids = cmd.get("strategy_ids")
                    strategy_mode = cmd.get("strategy_mode")  # e.g., 'top5'
                    start = cmd.get("start")  # 'YYYY-MM-DD'
                    end = cmd.get("end")      # 'YYYY-MM-DD'
                    capital = cmd.get("capital")  # numeric
                    
                    if cabinet_task and not cabinet_task.done():
                        cabinet_task.cancel()
                    start_new_backtest_report(stock_code, strategy_id, {
                        "stock_code": stock_code,
                        "strategy_id": strategy_id,
                        "strategy_ids": strategy_ids,
                        "strategy_mode": strategy_mode,
                        "start": start,
                        "end": end,
                        "capital": capital
                    })
                        
                    cabinet_task = asyncio.create_task(run_backtest_task(stock_code, strategy_id, strategy_mode, start, end, capital, strategy_ids))

                elif cmd.get("type") == "ping":
                    await websocket.send_json({
                        "type": "pong",
                        "data": {
                            "ts": cmd.get("ts"),
                            "server_ts": datetime.now().isoformat(timespec="seconds")
                        }
                    })
                
                elif cmd.get("type") == "switch_strategy":
                    # Handle strategy switch
                    strategy_id = cmd.get("id")
                    strategy_ids = cmd.get("ids")
                    selected = strategy_ids if strategy_ids else strategy_id
                    print(f"Switching to strategy: {selected}")
                    if current_cabinet:
                        current_cabinet.set_active_strategies(selected if selected else 'all')
                
                elif cmd.get("type") == "stop_simulation":
                     if cabinet_task and not cabinet_task.done():
                         print("Stopping Cabinet Task...")
                         cabinet_task.cancel()
                         await manager.broadcast({"type": "system", "data": {"msg": "内阁监控已手动停止"}})
                
                elif cmd.get("type") == "stop_backtest":
                    if cabinet_task and not cabinet_task.done():
                        print("Stopping Backtest Task...")
                        cabinet_task.cancel()
                        if current_backtest_report and str(current_backtest_report.get("status", "")).lower() == "running":
                            cancel_current_backtest_report("backtest task cancelled by user")
                        await manager.broadcast({"type": "system", "data": {"msg": "回测已手动终止"}})
                    
            except Exception as e:
                print(f"Command Error: {e}")
                
    except WebSocketDisconnect:
        manager.disconnect(websocket)
    except Exception:
        manager.disconnect(websocket)
    finally:
        manager.disconnect(websocket)

async def run_cabinet_task(stock_code):
    """Wrapper to run cabinet live loop"""
    if not is_live_enabled():
        await manager.broadcast({"type": "system", "data": {"msg": "Live功能已在配置中关闭，无法启动监控"}})
        return
    print(f"Starting Cabinet Task for {stock_code}")
    
    # Reload config
    config = ConfigLoader.reload()
    
    # Initialize
    global current_provider_source
    provider_source = current_provider_source or config.get("data_provider.source", "default")
    current_provider_source = provider_source
    
    cab = LiveCabinet(
        stock_code=stock_code,
        provider_type=provider_source,
        event_callback=emit_event_to_ws
    )
    
    global current_cabinet
    current_cabinet = cab
    
    try:
        await cab.run_live()
    except asyncio.CancelledError:
        print("Cabinet Task Cancelled")

async def run_backtest_task(stock_code, strategy_id, strategy_mode=None, start=None, end=None, capital=None, strategy_ids=None):
    """Wrapper to run backtest"""
    logger.info(
        "Starting Backtest params: stock_code=%s strategy_id=%s strategy_ids=%s strategy_mode=%s start=%s end=%s capital=%s",
        stock_code,
        strategy_id,
        strategy_ids,
        strategy_mode,
        start,
        end,
        capital,
    )
    initial_capital = float(capital) if capital is not None else 1000000.0
    
    cab = BacktestCabinet(
        stock_code=stock_code,
        strategy_id=strategy_id,
        initial_capital=initial_capital,
        event_callback=emit_event_to_ws,
        strategy_mode=strategy_mode,
        strategy_ids=strategy_ids
    )
    
    try:
        from datetime import datetime
        start_dt = None
        end_dt = None
        if start:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
        if end:
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        await cab.run(start_date=start_dt, end_date=end_dt)
        if current_backtest_report and current_backtest_report.get("status") == "running" and not current_backtest_report.get("summary"):
            fail_current_backtest_report("backtest finished without report summary")
    except asyncio.CancelledError:
        print("Backtest Task Cancelled")
        if current_backtest_report and str(current_backtest_report.get("status", "")).lower() == "running":
            cancel_current_backtest_report("backtest task cancelled")
    except Exception as e:
        logger.error(f"run_backtest_task failed: {e}", exc_info=True)
        fail_current_backtest_report(str(e))

async def emit_event_to_ws(event_type, data):
    global latest_backtest_result, latest_strategy_reports, current_backtest_report, current_backtest_progress, current_backtest_trades
    if event_type == "backtest_result":
        latest_backtest_result = data
        if current_backtest_report is not None:
            current_backtest_report["summary"] = data
            current_backtest_report["ranking"] = data.get("ranking", [])
            current_backtest_report["status"] = "success"
            current_backtest_report["error_msg"] = None
            current_backtest_report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            finalize_current_backtest_report()
        current_backtest_progress = {"progress": 100, "current_date": "Done"}
    elif event_type == "backtest_progress":
        current_backtest_progress = data
    elif event_type == "backtest_failed":
        msg = data.get("msg") if isinstance(data, dict) else str(data)
        fail_current_backtest_report(msg)
        current_backtest_progress = {"progress": current_backtest_progress.get("progress", 0), "current_date": "Failed"}
    elif event_type == "backtest_strategy_report":
        sid = str(data.get("strategy_id", ""))
        if sid:
            latest_strategy_reports[sid] = data
            if current_backtest_report is not None:
                current_backtest_report["strategy_reports"][sid] = data
    elif event_type == "backtest_trade":
        if isinstance(data, dict):
            current_backtest_trades.append({
                "dt": str(data.get("dt", "")),
                "strategy": str(data.get("strategy", "")),
                "code": str(data.get("code", "")),
                "dir": str(data.get("dir", "")),
                "price": float(data.get("price", 0.0) or 0.0),
                "qty": int(data.get("qty", 0) or 0)
            })
    # print(f"Emit: {event_type}")
    payload = {
        "type": event_type,
        "data": data
    }
    await manager.broadcast(payload)

@app.on_event("startup")
async def startup_event():
    global history_sync_scheduler_task
    logger.info("Initializing Cabinet Server...")
    load_report_history()
    _warmup_classic_pattern_thumbs()
    
    # Log registered routes
    logger.info("--- Registered API Endpoints ---")
    for route in app.routes:
        if hasattr(route, "methods"):
            logger.info(f"{route.methods} {route.path}")
    logger.info("--------------------------------")
    
    strategies = strategy_factory_module.create_strategies()
    logger.info(f"Loaded {len(strategies)} Strategies: {[s.name for s in strategies]}")
    cfg = ConfigLoader.reload()
    if bool(cfg.get("history_sync.scheduler_enabled", False)):
        history_sync_scheduler_task = asyncio.create_task(_history_sync_scheduler_loop())
    logger.info("Server Started. Access dashboard at http://localhost:8000")

@app.on_event("shutdown")
async def shutdown_event():
    global history_sync_scheduler_task
    if cabinet_task:
        cabinet_task.cancel()
    if history_sync_scheduler_task and not history_sync_scheduler_task.done():
        history_sync_scheduler_task.cancel()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        ws_ping_interval=20.0,
        ws_ping_timeout=180.0,
        ws_max_queue=1024
    )
