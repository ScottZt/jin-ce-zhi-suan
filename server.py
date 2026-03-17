
import asyncio
import json
import os
import importlib
import sys
import math
import numbers
from datetime import datetime
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, BackgroundTasks, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from typing import Optional
from src.core.live_cabinet import LiveCabinet
from src.core.backtest_cabinet import BacktestCabinet
from src.utils.config_loader import ConfigLoader
import src.strategies.strategy_factory as strategy_factory_module
from src.utils.stock_manager import stock_manager

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
report_history = []
REPORTS_DIR = os.path.join("data", "reports")
REPORTS_FILE = os.path.join(REPORTS_DIR, "backtest_reports.json")

# Config
config = ConfigLoader()

def is_live_enabled():
    cfg = ConfigLoader.reload()
    return bool(cfg.get("system.enable_live", True))

def load_report_history():
    global report_history, latest_backtest_result, latest_strategy_reports
    os.makedirs(REPORTS_DIR, exist_ok=True)
    if not os.path.exists(REPORTS_FILE):
        report_history = []
        return
    try:
        with open(REPORTS_FILE, "r", encoding="utf-8") as f:
            payload = json.load(f)
        report_history = payload.get("reports", [])
        if report_history:
            latest = report_history[0]
            latest_backtest_result = latest.get("summary")
            latest_strategy_reports = latest.get("strategy_reports", {})
    except Exception as e:
        logger.error(f"Failed to load report history: {e}")
        report_history = []

def persist_report_history():
    os.makedirs(REPORTS_DIR, exist_ok=True)
    with open(REPORTS_FILE, "w", encoding="utf-8") as f:
        json.dump({"reports": report_history}, f, ensure_ascii=False, indent=2, default=str)

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

def start_new_backtest_report(stock_code, strategy_id):
    global current_backtest_report, latest_backtest_result, latest_strategy_reports
    report_id = str(int(asyncio.get_event_loop().time() * 1000))
    current_backtest_report = {
        "report_id": report_id,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "stock_code": stock_code,
        "strategy_id": strategy_id,
        "summary": None,
        "ranking": [],
        "strategy_reports": {}
    }
    latest_backtest_result = None
    latest_strategy_reports = {}

def finalize_current_backtest_report():
    global report_history, current_backtest_report
    if not current_backtest_report:
        return
    report_history = [r for r in report_history if r.get("report_id") != current_backtest_report.get("report_id")]
    report_history.insert(0, current_backtest_report)
    persist_report_history()

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

class LiveRequest(BaseModel):
    stock_code: str = "600036.SH"

class StrategySwitchRequest(BaseModel):
    strategy_id: Optional[str] = None
    strategy_ids: Optional[list[str]] = None

class SourceSwitchRequest(BaseModel):
    source: str

class ConfigUpdateRequest(BaseModel):
    config: dict

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
        load_report_history()
        for r in report_history if isinstance(report_history, list) else []:
            if not isinstance(r, dict):
                continue
            if str(r.get("report_id")) == str(report_id):
                summary = r.get("summary") if isinstance(r.get("summary"), dict) else None
                ranking = summary.get("ranking", []) if summary else []
                strategy_reports = r.get("strategy_reports") if isinstance(r.get("strategy_reports"), dict) else {}
                reports = [v for v in strategy_reports.values() if isinstance(v, dict)]
                reports = sorted(reports, key=lambda x: str(x.get("strategy_id", "")))
                payload = {"summary": summary, "ranking": ranking, "strategy_reports": reports}
                payload = _sanitize_non_finite(payload)
                safe_payload = _safe_json_obj(payload)
                if isinstance(safe_payload, dict):
                    return safe_payload
                return {"summary": None, "ranking": [], "strategy_reports": []}
        raise HTTPException(status_code=404, detail="report not found")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"/api/report/{report_id} failed: {e}", exc_info=True)
        return {"summary": None, "ranking": [], "strategy_reports": []}

# --- Control Endpoints for External Systems (e.g. OpenClaw) ---
@app.post("/api/control/start_backtest")
async def api_start_backtest(req: BacktestRequest):
    """Start a backtest task (useful for OpenClaw API calls)"""
    global cabinet_task
    if cabinet_task and not cabinet_task.done():
        cabinet_task.cancel()
    start_new_backtest_report(req.stock_code, req.strategy_id)
    cabinet_task = asyncio.create_task(run_backtest_task(req.stock_code, req.strategy_id))
    return {"status": "success", "msg": f"Backtest started for {req.stock_code}"}

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
        await manager.broadcast({"type": "system", "data": {"msg": "Task stopped via API"}})
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
        "live_enabled": is_live_enabled()
    }


class ConnectionManager:
    def __init__(self):
        self.active_connections: list[WebSocket] = []

    async def connect(self, websocket: WebSocket):
        await websocket.accept()
        self.active_connections.append(websocket)

    def disconnect(self, websocket: WebSocket):
        self.active_connections.remove(websocket)

    async def broadcast(self, message: dict):
        for connection in self.active_connections:
            try:
                await connection.send_json(message)
            except Exception as e:
                print(f"WS Error: {e}")

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
                    start_new_backtest_report(stock_code, strategy_id)
                        
                    cabinet_task = asyncio.create_task(run_backtest_task(stock_code, strategy_id, strategy_mode, start, end, capital, strategy_ids))
                
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
                    
            except Exception as e:
                print(f"Command Error: {e}")
                
    except WebSocketDisconnect:
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
    print(f"Starting Backtest for {stock_code}")
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
    except asyncio.CancelledError:
        print("Backtest Task Cancelled")

async def emit_event_to_ws(event_type, data):
    global latest_backtest_result, latest_strategy_reports, current_backtest_report
    if event_type == "backtest_result":
        latest_backtest_result = data
        if current_backtest_report is not None:
            current_backtest_report["summary"] = data
            current_backtest_report["ranking"] = data.get("ranking", [])
            finalize_current_backtest_report()
    elif event_type == "backtest_strategy_report":
        sid = str(data.get("strategy_id", ""))
        if sid:
            latest_strategy_reports[sid] = data
            if current_backtest_report is not None:
                current_backtest_report["strategy_reports"][sid] = data
    # print(f"Emit: {event_type}")
    payload = {
        "type": event_type,
        "data": data
    }
    await manager.broadcast(payload)

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Cabinet Server...")
    load_report_history()
    
    # Log registered routes
    logger.info("--- Registered API Endpoints ---")
    for route in app.routes:
        if hasattr(route, "methods"):
            logger.info(f"{route.methods} {route.path}")
    logger.info("--------------------------------")
    
    strategies = strategy_factory_module.create_strategies()
    logger.info(f"Loaded {len(strategies)} Strategies: {[s.name for s in strategies]}")
    logger.info("Server Started. Access dashboard at http://localhost:8000")

@app.on_event("shutdown")
async def shutdown_event():
    if cabinet_task:
        cabinet_task.cancel()

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
