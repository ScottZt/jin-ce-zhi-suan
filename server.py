
import asyncio
import json
import os
import importlib
import sys
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
latest_backtest_result = None
latest_strategy_reports = {}
current_backtest_report = None
report_history = []
REPORTS_DIR = os.path.join("data", "reports")
REPORTS_FILE = os.path.join(REPORTS_DIR, "backtest_reports.json")

# Config
config = ConfigLoader()

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
    strategy_id: str

# --- Routes ---

@app.get("/")
async def get_dashboard():
    return HTMLResponse(content=open("dashboard.html", "r", encoding="utf-8").read())

@app.get("/report")
async def get_report_page():
    return HTMLResponse(content=open("backtest_report.html", "r", encoding="utf-8").read())

@app.get("/api/search")
async def search_stocks(q: str = ""):
    """Search stocks by code, name, or pinyin"""
    return {"results": stock_manager.search(q)}

@app.get("/api/report/latest")
async def api_latest_report():
    ranking = []
    summary = latest_backtest_result
    strategy_reports = latest_strategy_reports
    if not summary and report_history:
        summary = report_history[0].get("summary")
        strategy_reports = report_history[0].get("strategy_reports", {})
    if summary:
        ranking = summary.get("ranking", [])
    reports = list(strategy_reports.values())
    reports = sorted(reports, key=lambda x: str(x.get("strategy_id", "")))
    return {
        "summary": summary,
        "ranking": ranking,
        "strategy_reports": reports
    }

@app.get("/api/report/history")
async def api_report_history():
    items = []
    for r in report_history:
        summary = r.get("summary") or {}
        items.append({
            "report_id": r.get("report_id"),
            "created_at": r.get("created_at"),
            "stock_code": r.get("stock_code") or summary.get("stock"),
            "period": summary.get("period"),
            "total_trades": summary.get("total_trades", 0)
        })
    return {"reports": items}

@app.get("/api/report/{report_id}")
async def api_report_detail(report_id: str):
    for r in report_history:
        if str(r.get("report_id")) == str(report_id):
            summary = r.get("summary")
            ranking = []
            if summary:
                ranking = summary.get("ranking", [])
            reports = list((r.get("strategy_reports") or {}).values())
            reports = sorted(reports, key=lambda x: str(x.get("strategy_id", "")))
            return {"summary": summary, "ranking": ranking, "strategy_reports": reports}
    raise HTTPException(status_code=404, detail="report not found")

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
    if current_cabinet:
        current_cabinet.set_active_strategies(req.strategy_id)
        return {"status": "success", "msg": f"Strategy switched to {req.strategy_id}"}
    return {"status": "error", "msg": "No active cabinet running"}

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
        "active_cabinet_type": type(current_cabinet).__name__ if current_cabinet else None
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
                    strategy_mode = cmd.get("strategy_mode")  # e.g., 'top5'
                    start = cmd.get("start")  # 'YYYY-MM-DD'
                    end = cmd.get("end")      # 'YYYY-MM-DD'
                    capital = cmd.get("capital")  # numeric
                    
                    if cabinet_task and not cabinet_task.done():
                        cabinet_task.cancel()
                    start_new_backtest_report(stock_code, strategy_id)
                        
                    cabinet_task = asyncio.create_task(run_backtest_task(stock_code, strategy_id, strategy_mode, start, end, capital))
                
                elif cmd.get("type") == "switch_strategy":
                    # Handle strategy switch
                    strategy_id = cmd.get("id")
                    print(f"Switching to strategy: {strategy_id}")
                    if current_cabinet:
                        current_cabinet.set_active_strategies(strategy_id)
                
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
    print(f"Starting Cabinet Task for {stock_code}")
    
    # Reload config
    config = ConfigLoader.reload()
    
    # Initialize
    provider_source = config.get("data_provider.source", "default")
    
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

async def run_backtest_task(stock_code, strategy_id, strategy_mode=None, start=None, end=None, capital=None):
    """Wrapper to run backtest"""
    print(f"Starting Backtest for {stock_code}")
    
    cab = BacktestCabinet(
        stock_code=stock_code,
        strategy_id=strategy_id,
        event_callback=emit_event_to_ws,
        strategy_mode=strategy_mode
    )
    
    try:
        from datetime import datetime
        start_dt = None
        end_dt = None
        if start:
            start_dt = datetime.strptime(start, "%Y-%m-%d")
        if end:
            end_dt = datetime.strptime(end, "%Y-%m-%d")
        if capital:
            cab.revenue = cab.revenue.__class__(float(capital))
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
