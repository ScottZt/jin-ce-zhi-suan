<div align="center">
  <img src="./logo.png" alt="金策智算 Logo" width="140" />
  <h1>金策智算 · 智能投研决策系统</h1>
  <p>基于唐朝的三省六部制度建立的量化系统，分权协同、风控闭环、强化回测与执行。</p>
</div>

<p align="center">
  <img alt="Python" src="https://img.shields.io/badge/Python-3.8%2B-3776AB?logo=python&logoColor=white" />
  <img alt="Framework" src="https://img.shields.io/badge/API-FastAPI-009688?logo=fastapi&logoColor=white" />
  <img alt="Frontend" src="https://img.shields.io/badge/UI-Dashboard-0A66C2?logo=googlechrome&logoColor=white" />
  <img alt="Status" src="https://img.shields.io/badge/Status-Active-success" />
  <img alt="Market" src="https://img.shields.io/badge/Market-A--Share-red" />
</p>

## 目录

- [项目简介](#项目简介)
- [核心特性](#核心特性)
- [架构设计](#架构设计)
- [项目结构](#项目结构)
- [快速开始](#快速开始)
- [数据准备](#数据准备)
- [数据源与使用条件](#数据源与使用条件)
- [全局回测与实盘监控模板](#全局回测与实盘监控模板)
- [安全基线](#安全基线)
- [已知限制](#已知限制)
- [Roadmap](#roadmap)
- [贡献指南](#贡献指南)

## 项目简介

本项目采用“三省六部”思想构建量化系统，把**策略生成、风控审核、执行清算**分层解耦，支持A股以下核心场景：

- 历史回测与报告输出
- 实盘监控与风控拦截
- 多策略统一管理（内置 + 自定义）
- Web 面板配置与任务控制

<img width="2397" height="1343" alt="image" src="https://github.com/user-attachments/assets/01f37497-e060-4755-a44b-03f43c8dc4e6" />


## 核心特性

- 多策略并行：内置策略与用户策略统一纳管
- 风控优先：门下省一票否决机制，覆盖止损、回撤、仓位约束
- 回测闭环：从数据获取、信号执行到结果分析全链路打通
- 数据源可切换：AkShare / Tushare / 默认 API / MySQL / PostgreSQL
- 可视化运维：`server.py + dashboard.html` 提供操作面板

<img width="2508" height="1431" alt="image" src="https://github.com/user-attachments/assets/dce52494-76c8-4c4b-ba22-772dd9cb8e83" />


![](C:\Users\scott\AppData\Roaming\marktext\images\2026-03-23-22-14-57-image.png)

## 架构设计(智能体智能）

### 三省（决策主链路）

- 太子院：数据前置校验与分发
- 中书省：策略信号生成
- 门下省：风控审核与拦截
- 尚书省：执行调度与资金清算

### 六部（职能部门）

- 吏部：策略注册与生命周期管理
- 户部：现金、成本、净值核算
- 礼部：业绩报表与策略排行
- 兵部：撮合执行与交易管理
- 刑部：违规记录与风险事件
- 工部：行情清洗与指标计算

### 流程图

```mermaid
flowchart LR
  A[行情数据源] --> B[太子院 CrownPrince]
  B --> C[中书省 ZhongshuSheng]
  C --> D[门下省 MenxiaSheng]
  D -->|通过| E[尚书省 ShangshuSheng]
  D -->|否决| F[刑部 XingBuJustice]
  E --> G[兵部 BingBuWar]
  E --> H[户部 HuBuRevenue]
  E --> I[礼部 LiBuRites]
```

## 项目结构

```text
.
├─src/
│  ├─core/            # 三省核心流程
│  ├─ministries/      # 六部职能实现
│  ├─strategies/      # 内置与自定义策略管理
│  ├─strategy_intent/ # 策略意图解析与生成
│  └─utils/           # 配置、指标、数据源封装
├─data/               # 历史数据、策略库、报告数据
├─dashboard.html      # Web 面板
├─server.py           # FastAPI 服务入口
├─main.py             # 回测入口
├─run_live.py         # 实盘监控入口
└─run_backtest.py     # 命令行回测入口
```

## 快速开始

### 1. 环境要求

- Python 3.8+
- 建议使用虚拟环境

### 2. 安装依赖

```bash
pip install -r requirements.txt
pip install tushare akshare fastapi uvicorn
```

### 3. 配置说明

- 主配置：`config.json`
- 本地覆盖配置：`config.private.json`（可选）

系统会先加载 `config.json`，再自动用 `config.private.json` 进行覆盖。

`config.private.json` 示例：

```json
{
  "data_provider": {
    "tushare_token": "your_token",
    "default_api_key": "your_api_key",
    "llm_api_key": "your_llm_key",
    "strategy_llm_api_key": "your_strategy_llm_key"
  }
}
```

维护方式说明：

- 推荐直接维护 `config.private.json`，便于版本隔离与本地管理。
- 也可以在前端配置中心维护密钥字段，效果等价。
- 当前系统已实现“保存分流”：前端保存时普通配置写入 `config.json`，密钥字段写入 `config.private.json`。
- 若本地不存在 `config.private.json`，首次在前端保存密钥后会自动创建该文件。
- 自定义策略也支持“私有优先读取”：若存在 `data/strategies/custom_strategies.private.json`，系统会优先读取并写入该文件；否则回退到 `data/strategies/custom_strategies.json`。

推荐做法（多机器一致）：

- 建议将私有文件放到仓库外目录，例如：`D:\04.量化\private-data`
- 可直接在 `config.json` 中配置以下路径（无需每次敲命令）：
  - `system.private_config_path`
  - `system.private_strategy_path`
- 建议配置如下环境变量（每台机器各自设置一次）：
  - `CONFIG_PRIVATE_PATH=D:\04.量化\private-data\config.private.json`
  - `CUSTOM_STRATEGIES_PRIVATE_PATH=D:\04.量化\private-data\strategies\custom_strategies.private.json`
  - `CUSTOM_STRATEGIES_WRITE_PRIVATE=1`

PowerShell 示例：

```powershell
New-Item -ItemType Directory -Force -Path "D:\04.量化\private-data\strategies" | Out-Null
setx CONFIG_PRIVATE_PATH "D:\04.量化\private-data\config.private.json"
setx CUSTOM_STRATEGIES_PRIVATE_PATH "D:\04.量化\private-data\strategies\custom_strategies.private.json"
setx CUSTOM_STRATEGIES_WRITE_PRIVATE "1"
```

说明：

- 代码拉取到新机器后，不会自动带上私有文件，需要你自行放置到上述路径。
- 路径优先级：环境变量 > `config.json` 路径配置 > 项目默认路径。
- 新版 `server.py` 启动时会检查私有配置与私有策略路径，缺失会在日志中给出明确原因与修复建议。
- `config.private.json` 必须保存为 `UTF-8`（无 BOM）；若使用 `UTF-8 with BOM`，加载会失败并表现为 `default_api_key`、`tushare_token` 为空。

### 4. 启动方式

回测模式：

```bash
python main.py
```

命令行回测：

```bash
python run_backtest.py --stock 600036.SH --start 2025-01-01 --end 2025-12-31 --capital 1000000
```

实盘监控：

```bash
python run_live.py
```

启动 Web 面板（实际只需要启动server，剩下的都会启动）：

```bash
python server.py
```

命令行临时指定端口：

```bash
python server.py --prot 8001
```

可自定义启动地址与端口（优先级：环境变量 > `config.json`）：

```bash
$env:SERVER_HOST="0.0.0.0"
$env:SERVER_PORT="9000"
python server.py
```

也可在 `config.json` 的 `system.server_host`、`system.server_port` 中配置。

前台配置中心可以进行具体的配置

![](C:\Users\scott\AppData\Roaming\marktext\images\2026-03-23-22-16-35-image.png)

## 数据准备

- 本仓库默认不提供完整历史数据，`data/history/` 被 `.gitignore` 忽略，克隆后通常为空或仅少量样例。
- 若需完整回测数据，请联系仓库维护者。
- 如果你使用默认 API 数据源，系统会从 `data_provider.default_api_url` 拉取行情；若该服务不可用，回测将拿不到数据。
- 若你使用 Tushare/AkShare，可直接联网拉取，不依赖本地 `data/history` 全量文件。
- 历史差异同步功能依赖三项配置同时可用：`default_api_url`、`default_api_key`、`tushare_token`。

## 数据源与使用条件

| 数据源         | 配置项                                                                    | 使用条件                   | 典型用途         |
| ----------- | ---------------------------------------------------------------------- | ---------------------- | ------------ |
| default API | `data_provider.source=default` + `default_api_url` + `default_api_key` | 需要可访问的私有/自建行情服务        | 统一分钟线、批量回测   |
| Tushare     | `data_provider.source=tushare` + `tushare_token`                       | 需要 Tushare Token 与网络连通 | 标准化行情获取、历史补数 |
| AkShare     | `data_provider.source=akshare`                                         | 通常无需 Token，但依赖网络与上游可用性 | 快速验证、轻量使用    |
| MySQL       | `data_provider.source=mysql` + `mysql_*` 配置                           | 需安装 `pymysql` 并可直连 MySQL | 本地库直读、低延迟回测 |
| PostgreSQL  | `data_provider.source=postgresql` + `postgres_*` 配置                   | 需安装 `psycopg2-binary` 并可直连 PostgreSQL | 本地库直读、低延迟回测 |

说明：

- 实盘入口 `run_live.py` 支持 `TUSHARE_TOKEN` 环境变量覆盖配置文件。
- 未提供 Tushare Token 且选择 tushare 时，会自动回退到 akshare。
- MySQL 直连默认读取表：`dat_1mins/dat_5mins/dat_10mins/dat_15mins/dat_30mins/dat_60mins/dat_day`，可在 `config.json` 覆盖。
- MySQL 支持连接池与分页批量读取：`mysql_pool_size`、`mysql_pool_wait_timeout_sec`、`mysql_query_page_size`，用于大区间回测提速。
- PostgreSQL 直连默认读取表：`dat_1mins/dat_5mins/dat_10mins/dat_15mins/dat_30mins/dat_60mins/dat_day`，可在 `config.json` 覆盖。
- PostgreSQL 支持连接池与分页批量读取：`postgres_pool_size`、`postgres_pool_wait_timeout_sec`、`postgres_query_page_size`，用于大区间回测提速。
- 回测支持将分钟K线缓存落库到数据库：`data_provider.backtest_cache_db_source` 可设为 `mysql` 或 `postgresql`。
- 增量同步支持两种写入模式：`history_sync.write_mode=api`（写入 `default_api_url` 对应服务）或 `history_sync.write_mode=direct_db`（直连 `mysql/postgresql`）。
- 直连数据库模式下，目标库由 `history_sync.direct_db_source` 指定，可选 `mysql` / `postgresql`。
- PostgreSQL 落库使用 `ON CONFLICT (code, trade_time)`，目标表需具备 `(code, trade_time)` 唯一约束。
- MySQL 落库使用 `ON DUPLICATE KEY UPDATE`，目标表需具备可触发冲突更新的唯一键（建议 `(code, trade_time)`）。
- 增量同步时间窗口新增两种模式：`history_sync.time_mode=lookback`（按 `lookback_days` 回看最近 N 天）与 `history_sync.time_mode=custom`（使用 `custom_start_time/custom_end_time` 固定区间）。
- 当请求或配置中显式传入 `start_time/end_time` 时，优先使用显式时间，`time_mode` 仅在未显式传时间时生效。
- `history_sync.intraday_mode=true` 时，未显式传时间将使用“当日（或最近交易日）09:30:00~15:00:00”窗口；默认建议关闭，仅在盘中补数场景启用。
- `history_sync.session_only=true`（默认）时，分钟线源数据会过滤为交易时段 `09:30:00~15:00:00`，避免写入非交易分钟。

#### 增量同步时间配置示例

```json
"history_sync": {
  "time_mode": "lookback",
  "lookback_days": 20,
  "custom_start_time": "",
  "custom_end_time": "",
  "session_only": true,
  "intraday_mode": false
}
```

- 最近 N 天模式：`time_mode=lookback` + `lookback_days=N`
- 自定义区间模式：`time_mode=custom` + `custom_start_time=YYYY-MM-DD HH:MM:SS` + `custom_end_time=YYYY-MM-DD HH:MM:SS`
- API `/api/history_sync/run` 与 `/api/history_sync/scheduler/start` 同步支持上述字段：`time_mode/custom_start_time/custom_end_time/session_only/intraday_mode`

#### 自定义开始/结束时间窗模板写法

- 固定格式：`YYYY-MM-DD HH:MM:SS`
- 推荐边界：开始时间写 `09:30:00`，结束时间写 `15:00:00`
- 生效前提：`time_mode` 必须是 `custom`
- 优先级：若同时传了 `start_time/end_time`，则以 `start_time/end_time` 为准

```json
"history_sync": {
  "time_mode": "custom",
  "custom_start_time": "2026-03-01 09:30:00",
  "custom_end_time": "2026-03-31 15:00:00",
  "session_only": true,
  "intraday_mode": false
}
```

接口请求体模板：

```json
{
  "time_mode": "custom",
  "custom_start_time": "2026-03-01 09:30:00",
  "custom_end_time": "2026-03-31 15:00:00",
  "session_only": true,
  "intraday_mode": false
}
```

#### 历史数据源表结构参照

- MySQL 建表参考：项目根目录 `历史数据源表结构.sql`。
- 该脚本已包含分钟/日线表及 `uk_code_trade_time(code, trade_time)` 唯一索引定义，可直接用于 MySQL 初始化。
- PostgreSQL 可按同名表结构迁移，并确保存在唯一约束 `(code, trade_time)`，以兼容缓存落库的 upsert 语义。

## 全局回测与实盘监控模板

- 全局默认参数、策略映射规则、实盘告警阈值模板见：[全局回测与实盘监控基线模板.md](./全局回测与实盘监控基线模板.md)
- 建议先按模板完成统一口径配置，再接入回测与实盘链路
- 默认数据源建议固定为 `default`，避免回测中途切换
- 建议采用“回测 -> 模拟盘 -> 小资金实盘”分级准入

## 安全基线

- 禁止将 Token / Key 写入 `config.json`
- 统一将密钥写入 `config.private.json` 或环境变量
- `.gitignore` 已忽略 `config.private.json` 与 `.env*`
- Web 面板中的密钥字段已改为密码框显示
- 配置保存会分流：普通配置写入 `config.json`，密钥字段写入 `config.private.json`
- 后端 `/api/config` 返回的密钥字段为脱敏值，请勿在公网暴露未鉴权服务

## 已知限制

- `main.py` 的回测时间区间默认写死在代码内（2024-01-01 到 2025-12-31），开箱即用但灵活性有限。
- 数据源切换依赖配置正确性，配置缺失时会出现“无有效数据，回测终止”。
- 仪表盘密钥字段为密码框，仅提供前端遮挡，不等同于后端鉴权与传输加密能力。
- 当前仓库未内置 CI 自动检查流程，提交前建议自行执行核心脚本与测试。

## Roadmap

- [x] 三省六部核心交易链路
- [x] 回测 + 实盘双模式
- [x] 策略管理与自定义策略存储
- [x] Web 配置与任务控制面板
- [ ] 更完善的 CI（测试、格式检查、发布流程）
- [ ] 更多标准化样例策略与基准报告

## 授权说明

本项目采用 **“个人非商业免费 + 商业需授权”** 模式。

**免费使用范围（非商业）**
- 个人学习
- 学术研究
- 本地自用（不对外提供商业服务）

**以下行为必须事先取得作者书面商业授权**
- 售卖本项目或衍生版本
- 托管服务、SaaS、云端收费服务
- 二次包装后销售、分销、代理
- 任何直接或间接盈利部署

**商业授权联系**
- 联系方式：`zthx410@163.com`
- 说明：商业授权范围、费用与支持条款以双方签署协议为准。

<img width="1108" height="1513" alt="d02b159dbb826cdf9875c01aa06d9b63" src="https://github.com/user-attachments/assets/77a5a2cf-ae19-4f47-8b1d-0f25b1a9e15c" />

**免责声明**
- 本项目仅用于量化回测、本地数据处理与技术研究。
- 不构成投资建议，不荐股，不承诺收益。
- 使用本项目产生的一切风险（包括但不限于投资损失、数据损失、业务中断）由使用者自行承担。

> 详细条款请见仓库根目录 `LICENSE` 文件；如与商业协议冲突，以商业协议为准。


## 贡献指南

欢迎提交 Issue 和 PR。建议流程：

1. Fork 项目并创建功能分支
2. 保持提交粒度清晰，说明改动动机
3. 提交前确保核心脚本可运行
4. 通过 PR 描述测试方法与影响范围
