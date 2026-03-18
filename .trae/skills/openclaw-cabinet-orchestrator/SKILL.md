---
name: "openclaw-cabinet-orchestrator"
description: "Orchestrates this Cabinet backtest/live API workflow with robust polling and result parsing. Invoke when user asks OpenClaw to run backtests, track progress, or fetch reports."
---

# OpenClaw Cabinet Orchestrator

## Skill Purpose

此 Skill 用于让 OpenClaw 稳定调用本项目服务端 API，完成以下任务：

- 发起回测（支持全策略、单策略、策略组合）
- 追踪回测进度并动态判断是否卡死
- 拉取报告并结构化输出结果
- 在失败时返回可诊断的错误信息，而不是误判“服务未启动”
- 可选执行实盘启动与策略切换

本 Skill 重点解决的问题：

- 回测耗时随数据量变化，不能用固定轮询次数判断失败
- `is_running=false` 不代表一定有成功报告，需要结合 `status/error_msg`
- 禁止调用项目中不存在的接口，避免 OpenClaw 自行“猜路径”

---

## Environment & Base URL

- Base URL: `http://127.0.0.1:8000`
- 所有请求默认 `Content-Type: application/json`
- 所有时间建议使用 `YYYY-MM-DD`

---

## API Reference

### 1) System Status

#### GET `/api/status`

用途：查询当前任务状态与进度。

关键返回字段：

- `is_running`: 当前是否有任务在执行
- `progress.progress`: 回测进度（0-100）
- `progress.current_date`: 当前处理到的时间点
- `current_report_id`: 当前回测任务对应报告 ID
- `current_report_status`: `running | success | failed`
- `current_report_error`: 失败原因（若失败）
- `provider_source`: 当前数据源

示例：

```bash
curl -s http://127.0.0.1:8000/api/status
```

---

### 2) Start Backtest

#### POST `/api/control/start_backtest`

用途：启动回测任务。

请求体字段：

- `stock_code` (string): 如 `301227.SZ`
- `strategy_id` (string): `all` 或单策略 ID（如 `01`）
- `strategy_ids` (array[string], optional): 指定多策略（如 `["01","07"]`）
- `strategy_mode` (string, optional): 如 `top5`
- `start` (string, optional): 开始日期 `YYYY-MM-DD`
- `end` (string, optional): 结束日期 `YYYY-MM-DD`
- `capital` (number, optional): 初始资金

返回字段：

- `status`
- `msg`
- `report_id`（必须保存并用于后续查询）

示例（近一年全策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_backtest \
  -H "Content-Type: application/json" \
  -d '{
    "stock_code":"301227.SZ",
    "strategy_id":"all",
    "start":"2025-03-18",
    "end":"2026-03-18"
  }'
```

示例（策略 01+07）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_backtest \
  -H "Content-Type: application/json" \
  -d '{
    "stock_code":"301227.SZ",
    "strategy_id":"all",
    "strategy_ids":["01","07"],
    "start":"2025-03-18",
    "end":"2026-03-18",
    "capital":1000000
  }'
```

---

### 3) Get Latest Report

#### GET `/api/report/latest`

用途：获取最近一份报告（成功或失败上下文）。

关键字段：

- `report_id`
- `status`
- `error_msg`
- `summary`
- `ranking`
- `strategy_reports`

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/latest
```

---

### 4) Get Report History

#### GET `/api/report/history`

用途：查看报告历史与状态。

关键字段：

- `report_id`
- `created_at`
- `finished_at`
- `status`
- `error_msg`
- `stock_code`
- `period`
- `total_trades`

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/history
```

---

### 5) Get Report by ID

#### GET `/api/report/{report_id}`

用途：按启动返回的 `report_id` 精准获取报告详情。

关键字段：

- `report_id`
- `status`
- `error_msg`
- `request`
- `summary`
- `ranking`
- `strategy_reports`

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/<report_id>
```

---

### 6) Stop Task

#### POST `/api/control/stop`

用途：中止当前任务（重试前清理状态）。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/stop
```

---

### 7) Optional Live APIs

#### POST `/api/control/start_live`

用途：启动实盘模拟。

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_live \
  -H "Content-Type: application/json" \
  -d '{"stock_code":"600036.SH"}'
```

#### POST `/api/control/switch_strategy`

用途：切换策略。

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/switch_strategy \
  -H "Content-Type: application/json" \
  -d '{"strategy_id":"05"}'
```

---

## Execution Protocol

当用户说“开始回测并给结果”时，OpenClaw 必须按以下顺序执行：

1. 调用 `POST /api/control/start_backtest`
2. 记录完整 `report_id`（禁止截断、禁止转数字）
3. 每 3 秒轮询 `GET /api/status`
4. 判断：
   - 若 `current_report_status=failed`，直接输出失败原因并结束
   - 若 `is_running=true`，持续输出进度日志
   - 若 `is_running=false`，进入报告拉取
5. 优先调用 `GET /api/report/{report_id}`
6. 若 404，则回退：
   - `GET /api/report/history`（查是否存在该 id）
   - `GET /api/report/latest`（仅作为兜底）
7. 输出结构化报告

---

## Progress & Timeout Rules

采用动态超时，不用固定轮询次数：

- 维护 `last_progress_value` 与 `last_progress_update_time`
- 只要 `progress.progress` 增长，刷新 `last_progress_update_time`
- 若进度 90 秒无变化，判定“任务疑似卡住”
- 但如果 `current_report_status` 变成 `failed`，立即结束并报告错误

建议日志模板：

- `Backtest running: 42% @ 2025-09-08 14:35:00`
- `Backtest failed: <current_report_error>`
- `Backtest completed, report_id=<id>`

---

## Output Schema (Mandatory)

### A. Task Meta

- `report_id`
- `status`
- `error_msg`
- `stock`
- `period`

### B. Summary

- `total_trades`

### C. Ranking Table

- `rank`
- `strategy_id`
- `rating`
- `annualized_roi`
- `max_dd`
- `win_rate`
- `calmar`

### D. Strategy Reports

- `strategy_id`
- `total_return`
- `annual_return`
- `max_drawdown`
- `trade_count`
- `win_rate`
- `profit_ratio`

---

## Error Handling Rules

- 任一步骤非 2xx：打印 HTTP 状态码 + 响应体
- 启动回测失败：
  1) 先 `POST /api/control/stop`
  2) 再重试一次 `start_backtest`
  3) 仍失败则终止并输出完整错误日志
- `GET /api/report/{report_id}` 返回 404 时，不得直接判定服务异常，必须先走 history/latest 兜底流程
- 若 `status=failed`，优先输出 `error_msg` 及 `request` 参数用于复盘

---

## Strict Constraints

OpenClaw 禁止调用以下路径（本项目无此接口）：

- `/api/`
- `/reports`
- `/api/report/list`

OpenClaw 必须遵守：

- 不得使用历史旧 `report_id` 代替本次启动返回值
- 不得将 `report_id` 当作数字处理
- 不得在回测进行中就提前读取旧报告并当作本次结果

---

## Invocation Prompt Template

```text
现在开始执行“金策智算”回测流程：
1) POST /api/control/start_backtest 启动任务并获取 report_id
2) 每3秒 GET /api/status，跟踪 progress 和 current_report_status
3) 任务结束后优先 GET /api/report/{report_id}
4) 若404则依次回退 history 与 latest
5) 输出结构化结果（meta + summary + ranking + strategy_reports）
6) 全程打印每一步请求与响应要点
```
