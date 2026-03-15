
import asyncio
import json
import os
import importlib
import sys
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

# Config
config = ConfigLoader()

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

@app.get("/api/search")
async def search_stocks(q: str = ""):
    """Search stocks by code, name, or pinyin"""
    return {"results": stock_manager.search(q)}

# --- Control Endpoints for External Systems (e.g. OpenClaw) ---
@app.post("/api/control/start_backtest")
async def api_start_backtest(req: BacktestRequest):
    """Start a backtest task (useful for OpenClaw API calls)"""
    global cabinet_task
    if cabinet_task and not cabinet_task.done():
        cabinet_task.cancel()
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
                    
                    if cabinet_task and not cabinet_task.done():
                        cabinet_task.cancel()
                        
                    cabinet_task = asyncio.create_task(run_backtest_task(stock_code, strategy_id))
                
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

async def run_backtest_task(stock_code, strategy_id):
    """Wrapper to run backtest"""
    print(f"Starting Backtest for {stock_code}")
    
    cab = BacktestCabinet(
        stock_code=stock_code,
        strategy_id=strategy_id,
        event_callback=emit_event_to_ws
    )
    
    try:
        await cab.run()
    except asyncio.CancelledError:
        print("Backtest Task Cancelled")

async def emit_event_to_ws(event_type, data):
    # print(f"Emit: {event_type}")
    payload = {
        "type": event_type,
        "data": data
    }
    await manager.broadcast(payload)

@app.on_event("startup")
async def startup_event():
    logger.info("Initializing Cabinet Server...")
    
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
