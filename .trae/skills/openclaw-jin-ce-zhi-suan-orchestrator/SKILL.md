---
name: "openclaw-jin-ce-zhi-suan-orchestrator"
description: "通过金策智算HTTP接口执行回测/实盘编排与监控。用户要求启动任务、查进度、停止任务、切换策略/数据源、查询报告时必须调用。"
---

# OpenClaw 金策智算编排技能（中文版）

## 1) 技能目标与适用场景

本技能用于编排 `http://127.0.0.1:8000` 的金策智算服务，统一完成：

- 启动回测、启动实盘
- 任务进度监控
- 停止任务
- 切换策略与数据源
- 查询单个报告、历史报告、最新报告
- 触发报告 AI 复盘

当用户提出以上任何需求时，必须使用本技能，不要直接调用未登记接口。

## 2) 项目能力说明（给模型的业务认知）

金策智算项目具备以下核心能力：

- 多策略回测：支持单策略、多策略组合、策略模式参数
- 实盘监控：支持实时任务开启/停止与状态查询
- 报告体系：支持按 `report_id` 精确获取、历史列表、最新结果
- 配置与切换：支持运行中切换策略、切换数据源、热重载策略
- 可观测性：可通过 `progress.progress`、`progress.current_date` 监控回测推进

注意：回测耗时会明显受以下因素影响，时长波动属于正常现象：

- 回测区间长短（日期跨度）
- 数据周期（如分钟线通常比日线更慢）
- 策略数量（策略越多，计算越慢）
- 数据源响应速度与网络状态

## 3) 基础约束

- Base URL：`http://127.0.0.1:8000`
- Header：`Content-Type: application/json`
- 日期格式：`YYYY-MM-DD`
- `report_id` 全链路必须按字符串处理
- 禁止调用未文档化接口
- 输出语言默认中文

## 4) 已批准接口清单（逐个接口给出调用方法与示例）

### 4.1 状态接口

#### `GET /api/status`

用途：查询当前任务状态与进度。

关键字段：

- `is_running`
- `progress.progress`
- `progress.current_date`
- `current_report_id`
- `current_report_status`
- `current_report_error`
- `provider_source`
- `live_enabled`

示例：

```bash
curl -s http://127.0.0.1:8000/api/status
```

---

### 4.2 控制接口

#### `POST /api/control/start_backtest`

用途：启动回测任务。

请求体：

- `stock_code`：string，例 `301227.SZ`
- `strategy_id`：string，`all` 或单策略
- `strategy_ids`：string[]，可选
- `strategy_mode`：string，可选
- `start`：string，可选，`YYYY-MM-DD`
- `end`：string，可选，`YYYY-MM-DD`
- `capital`：number，可选

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_backtest \
  -H "Content-Type: application/json" \
  -d "{\"stock_code\":\"301227.SZ\",\"strategy_id\":\"all\",\"start\":\"2025-03-20\",\"end\":\"2026-03-20\",\"capital\":1000000}"
```

#### `POST /api/control/start_live`

用途：启动实盘监控任务。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/start_live \
  -H "Content-Type: application/json" \
  -d "{\"stock_code\":\"301227.SZ\"}"
```

#### `POST /api/control/stop`

用途：停止当前运行任务（回测或实盘）。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/stop
```

#### `POST /api/control/switch_strategy`

用途：运行时切换策略。

示例（单策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/switch_strategy \
  -H "Content-Type: application/json" \
  -d "{\"strategy_id\":\"01\"}"
```

示例（多策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/switch_strategy \
  -H "Content-Type: application/json" \
  -d "{\"strategy_ids\":[\"01\",\"05\",\"09\"]}"
```

#### `POST /api/control/set_source`

用途：切换数据源（`default` / `tushare` / `akshare`）。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/set_source \
  -H "Content-Type: application/json" \
  -d "{\"source\":\"tushare\"}"
```

#### `POST /api/control/reload_strategies`

用途：热重载策略实现。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/control/reload_strategies
```

---

### 4.3 报告接口

#### `GET /api/report/{report_id}`

用途：查询指定报告详情。

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/1742400000000-ab12
```

#### `GET /api/report/history`

用途：查询历史报告列表。

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/history
```

#### `GET /api/report/latest`

用途：查询最新报告。

示例：

```bash
curl -s http://127.0.0.1:8000/api/report/latest
```

#### `POST /api/report/{report_id}/ai_review`

用途：触发指定报告的 AI 复盘。

示例：

```bash
curl -s -X POST http://127.0.0.1:8000/api/report/1742400000000-ab12/ai_review
```

---

### 4.4 策略介绍与策略管理接口

#### `GET /api/strategies`

用途：获取当前可加载策略的基础列表（策略ID、策略名）。

适用场景：

- 用户问“当前有哪些策略”
- 启动回测前快速校验策略是否存在

示例：

```bash
curl -s http://127.0.0.1:8000/api/strategies
```

#### `GET /api/strategy_manager/list`

用途：获取策略管理器明细列表（含策略元信息、启用状态、来源、K线周期、评分字段）。

关键返回（示例字段）：

- `id`、`name`、`enabled`、`source`、`kline_type`
- `analysis_text`、`template_text`
- `score_total`、`rating`、`score_total_adjusted`
- `score_confidence`、`score_backtest_count`
- `score_annualized_roi_avg`、`score_max_dd_avg`、`score_trades_avg`

适用场景：

- 用户要看“策略管理器完整明细”
- 用户要做策略横向对比与筛选

示例：

```bash
curl -s http://127.0.0.1:8000/api/strategy_manager/list
```

#### `GET /api/strategy_manager/detail?strategy_id=01`

用途：直接获取单个策略详情（含策略元信息与评分字段），用于策略介绍、策略单卡详情、策略定位排障。

请求参数：

- `strategy_id`：必填，策略ID

示例：

```bash
curl -s "http://127.0.0.1:8000/api/strategy_manager/detail?strategy_id=01"
```

#### 策略介绍生成（智能分析）

- `POST /api/strategy_manager/analyze`：基于自然语言需求生成策略介绍、意图解释与代码
- `POST /api/strategy_manager/analyze_market`：基于市场状态生成策略介绍、意图解释与代码

示例（自然语言）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/strategy_manager/analyze \
  -H "Content-Type: application/json" \
  -d "{\"template_text\":\"趋势+回撤控制，偏稳健\",\"strategy_name\":\"稳健趋势策略\"}"
```

#### 策略修改与维护接口（已上线，可直接编排）

- `POST /api/strategy_manager/add`：新增策略（通常接 analyze 结果后落库）
- `POST /api/strategy_manager/update`：修改策略名称/代码/说明/来源/周期等
- `POST /api/strategy_manager/delete`：删除策略
- `POST /api/strategy_manager/toggle`：启用或禁用策略

示例（新增策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/strategy_manager/add \
  -H "Content-Type: application/json" \
  -d "{\"strategy_id\":\"98\",\"strategy_name\":\"稳健趋势策略\",\"class_name\":\"Strategy98\",\"code\":\"class Strategy98: pass\",\"analysis_text\":\"测试策略\"}"
```

示例（修改策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/strategy_manager/update \
  -H "Content-Type: application/json" \
  -d "{\"strategy_id\":\"98\",\"strategy_name\":\"稳健趋势策略V2\",\"analysis_text\":\"更新止损规则\"}"
```

示例（启停策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/strategy_manager/toggle \
  -H "Content-Type: application/json" \
  -d "{\"strategy_id\":\"98\",\"enabled\":true}"
```

示例（删除策略）：

```bash
curl -s -X POST http://127.0.0.1:8000/api/strategy_manager/delete \
  -H "Content-Type: application/json" \
  -d "{\"strategy_id\":\"98\"}"
```

---

### 4.5 策略介绍接口扩展规划（预留，未上线前禁止调用）

以下为建议新增接口，写入技能用于后续版本规划，不代表当前可调用：

- `GET /api/strategy_manager/summary`
  - 用途：获取轻量策略卡片（ID/名称/评级/近N次表现）
- `GET /api/strategy_manager/compare?ids=01,02,03`
  - 用途：按多策略返回对比视图（收益、回撤、胜率、交易次数）
- `GET /api/strategy_manager/score_trend?strategy_id=01`
  - 用途：输出策略评分时间序列，观察策略稳定性

## 5) 强制编排流程（回测）

1. 调用 `POST /api/control/start_backtest`
2. 必须校验返回 `status=success`，并记录 `report_id`（字符串）
3. 每 3 秒轮询 `GET /api/status`
4. 状态决策：
   - `current_report_status == "failed"`：立即失败返回
   - `is_running == true`：继续轮询
   - `is_running == false`：进入报告查询
5. 调用 `GET /api/report/{report_id}`
6. 若 404：按顺序回退
   - `GET /api/report/history`
   - `GET /api/report/latest`
7. 按统一 JSON 结构返回

## 5.1 强制编排流程（策略介绍/策略明细查询）

当用户请求“策略列表、策略管理器明细、单策略详情、策略说明”时，编排步骤如下：

1. 若是“策略列表”诉求：调用 `GET /api/strategies`
2. 若是“策略管理器明细”诉求：调用 `GET /api/strategy_manager/list`
3. 若是“单个策略详情”诉求：
   - 调 `GET /api/strategy_manager/detail?strategy_id=xx`
   - 若返回 `not_found`，明确告知用户策略不存在
4. 若是“策略介绍生成”诉求：
   - 调 `POST /api/strategy_manager/analyze` 或 `analyze_market`
5. 若是“修改策略/启停策略/删除策略”诉求：
   - 修改：`POST /api/strategy_manager/update`
   - 启停：`POST /api/strategy_manager/toggle`
   - 删除：`POST /api/strategy_manager/delete`
6. 任何失败都要走异常兜底回复，禁止只返回“处理中”

## 6) 进度播报规范（重点）

为了防止用户等待过久，必须执行双层节奏：

- **内部轮询节奏**：每 **3 秒**请求一次 `/api/status`
- **对用户播报节奏**：每 **90 秒**至少播报一次当前进度

30 秒播报内容建议包含：

- 当前进度百分比：`progress.progress`
- 当前推进日期：`progress.current_date`
- 当前报告状态：`current_report_status`
- 已运行时长（从 start_backtest 到当前）

即使进度值不变，也要每 90 秒向用户输出“仍在执行中”的进展说明。

## 7) 超时、等待与卡死治理

- HTTP 单次请求超时：建议 15~30 秒（按接口特性动态设置）
- 总任务等待：使用动态超时，不允许只靠固定重试次数
- 进度卡死判定：若 `progress.progress` 连续 90 秒无变化，返回 `TASK_STALLED`
- 一旦出现 `current_report_status=failed`，立即终止轮询并返回错误

## 8) 异常处理标准

- 任意非 2xx：记录完整请求上下文与错误摘要
- 启动回测失败：先调用 `POST /api/control/stop`，再重试一次
- 报告查询 404：必须执行回退链路后再判定失败
- 回退后仍无报告：返回 `PARTIAL_SUCCESS`，附带状态快照与完整日志
- 执行过程中任意异常（HTTPError/Timeout/JSON解析失败/字段缺失）：必须立即向用户输出“已知异常 + 当前阶段 + 下一步动作”，禁止静默失败
- 当进入异常分支时，必须给出最终兜底回复，至少包含：`trace_id`、`operation`、`status`、`error_msg`、`latest_status_snapshot`、`logs`
- 禁止无响应等待：超过 30 秒未成功推进时，仍需给用户一条“处理中/已重试/当前阻塞点”说明

## 9) 请求日志模板（每次HTTP调用都要输出）

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

日志要求：

- 同一用户任务内 `trace_id` 必须一致
- `response_preview` 仅保留关键字段，避免塞入超大 payload
- 出错时必须同时保留 `http_status`、`response_preview`、`error`
- 若出现异常，最后一条日志必须标记 `error` 且可直接用于向用户解释失败原因

## 10) 统一输出 JSON Schema

最终结果必须符合以下结构：

Final result must conform to this schema:

```json
{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "OpenClawJinCeZhiSuanResult",
  "type": "object",
  "required": ["trace_id", "operation", "status", "meta", "summary", "ranking", "strategy_reports", "logs"],
  "properties": {
    "trace_id": { "type": "string" },
    "operation": { "type": "string", "enum": ["start_backtest", "start_live", "stop_task", "fetch_report", "list_strategies", "get_strategy_detail", "analyze_strategy", "add_strategy", "update_strategy", "toggle_strategy", "delete_strategy"] },
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

## 11) 禁止接口

- `/api/`
- `/reports`
- `/api/report/list`
