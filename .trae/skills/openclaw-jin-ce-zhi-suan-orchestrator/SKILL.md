---
name: "openclaw-jin-ce-zhi-suan-orchestrator"
description: "Runs and monitors Jin-Ce-Zhi-Suan backtest/live workflows via HTTP APIs. Invoke when user asks OpenClaw to start runs, poll progress, stop tasks, or fetch reports."
---

# OpenClaw Jin-Ce-Zhi-Suan Orchestrator

## Enterprise Scope

This skill provides controlled orchestration for Jin-Ce-Zhi-Suan APIs at `http://127.0.0.1:8000`, with mandatory trace logs and normalized JSON output.

Use this skill when user asks to:

- start backtest / live run
- monitor progress
- stop running tasks
- switch strategy or source
- fetch latest or specific report

## Base Policy

- Base URL: `http://127.0.0.1:8000`
- Content-Type: `application/json`
- Date format: `YYYY-MM-DD`
- `report_id` must be treated as string end-to-end
- Never call undocumented endpoints

## Approved API Inventory

### Status

- `GET /api/status`
  - Required fields: `is_running`, `progress.progress`, `progress.current_date`, `current_report_id`, `current_report_status`, `current_report_error`, `provider_source`, `live_enabled`

### Control

- `POST /api/control/start_backtest`
  - Body:
    - `stock_code`: string
    - `strategy_id`: string (`all` or one strategy)
    - `strategy_ids`: string[] optional
    - `strategy_mode`: string optional
    - `start`: string optional
    - `end`: string optional
    - `capital`: number optional
  - Return: `status`, `msg`, `report_id`
- `POST /api/control/start_live`
- `POST /api/control/stop`
- `POST /api/control/switch_strategy`
- `POST /api/control/set_source`
- `POST /api/control/reload_strategies`

### Report

- `GET /api/report/{report_id}`
- `GET /api/report/history`
- `GET /api/report/latest`
- `POST /api/report/{report_id}/ai_review`

## Mandatory Orchestration Workflow

For backtest request:

1. Call `POST /api/control/start_backtest`
2. Assert response success and capture exact `report_id`
3. Poll `GET /api/status` every 3 seconds
4. Decision:
   - if `current_report_status == "failed"`: return failed with reason
   - if `is_running == true`: continue polling
   - if `is_running == false`: fetch report
5. Query `GET /api/report/{report_id}`
6. If 404: fallback to `GET /api/report/history`, then `GET /api/report/latest`
7. Return final payload using the unified JSON schema below

## Stall and Timeout Governance

- Use dynamic timeout, never fixed retry count only
- Track last `progress.progress` change time
- If no progress change for 90 seconds, return `TASK_STALLED` result
- If status becomes failed at any time, return immediately

## Request Log Template

For each HTTP call, output one structured request log record:

```json
{
  "trace_id": "oc-20260320-001",
  "step": 1,
  "timestamp": "2026-03-20T10:00:00Z",
  "method": "POST",
  "path": "/api/control/start_backtest",
  "query": {},
  "request_body": {
    "stock_code": "301227.SZ",
    "strategy_id": "all",
    "start": "2025-03-20",
    "end": "2026-03-20"
  },
  "http_status": 200,
  "duration_ms": 128,
  "response_preview": {
    "status": "success",
    "report_id": "1742400000000-ab12"
  },
  "error": null
}
```

Logging rules:

- `trace_id` must stay identical through one user task
- `response_preview` only keeps key fields, not full large payload
- on error, keep `http_status`, `response_preview`, and `error` message

## Unified Output JSON Schema

Final result must conform to this schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OpenClawJinCeZhiSuanResult",
  "type": "object",
  "required": ["trace_id", "operation", "status", "meta", "summary", "ranking", "strategy_reports", "logs"],
  "properties": {
    "trace_id": { "type": "string" },
    "operation": { "type": "string", "enum": ["start_backtest", "start_live", "stop_task", "fetch_report"] },
    "status": { "type": "string", "enum": ["SUCCESS", "FAILED", "TASK_STALLED", "PARTIAL_SUCCESS"] },
    "meta": {
      "type": "object",
      "required": ["report_id", "report_status", "error_msg", "stock", "period"],
      "properties": {
        "report_id": { "type": ["string", "null"] },
        "report_status": { "type": ["string", "null"] },
        "error_msg": { "type": ["string", "null"] },
        "stock": { "type": ["string", "null"] },
        "period": { "type": ["string", "null"] }
      },
      "additionalProperties": true
    },
    "summary": {
      "type": "object",
      "required": ["total_trades"],
      "properties": {
        "total_trades": { "type": ["number", "null"] },
        "total_return": { "type": ["number", "null"] },
        "annualized_return": { "type": ["number", "null"] },
        "max_drawdown": { "type": ["number", "null"] }
      },
      "additionalProperties": true
    },
    "ranking": {
      "type": "array",
      "items": {
        "type": "object",
        "properties": {
          "strategy_id": { "type": ["string", "null"] },
          "rating": { "type": ["string", "null"] },
          "annualized_roi": { "type": ["number", "null"] },
          "max_dd": { "type": ["number", "null"] },
          "win_rate": { "type": ["number", "null"] },
          "calmar": { "type": ["number", "null"] }
        },
        "additionalProperties": true
      }
    },
    "strategy_reports": {
      "type": "array",
      "items": { "type": "object", "additionalProperties": true }
    },
    "logs": {
      "type": "array",
      "items": {
        "type": "object",
        "required": ["step", "timestamp", "method", "path", "http_status", "duration_ms"],
        "properties": {
          "step": { "type": "integer" },
          "timestamp": { "type": "string" },
          "method": { "type": "string" },
          "path": { "type": "string" },
          "http_status": { "type": ["integer", "null"] },
          "duration_ms": { "type": ["number", "null"] },
          "response_preview": { "type": ["object", "null"] },
          "error": { "type": ["string", "null"] }
        },
        "additionalProperties": true
      }
    }
  },
  "additionalProperties": false
}
```

## Failure Handling Standard

- Any non-2xx: capture full error context in log item
- Start-backtest failure: call `POST /api/control/stop`, then retry once
- 404 on `/api/report/{report_id}`: must use fallback sequence before declaring failure
- If no report found after fallback, return `PARTIAL_SUCCESS` with logs and latest status snapshot

## Forbidden Endpoints

- `/api/`
- `/reports`
- `/api/report/list`

## Quick Commands

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_backtest -H "Content-Type: application/json" -d '{"stock_code":"301227.SZ","strategy_id":"all","start":"2025-03-20","end":"2026-03-20"}'
```

```bash
curl -s http://127.0.0.1:8000/api/status
```

```bash
curl -s http://127.0.0.1:8000/api/report/<report_id>
```
