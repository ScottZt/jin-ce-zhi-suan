# History Sync Stock List Refresh Status Design

## 1. Background

当前配置中心 `工部增量同步` 区域已经有 `更新股票池` 按钮，点击后会触发：

- 前端严格草稿取值
- 后端接口 `/api/history_sync/stock_list/refresh`
- 股票池文件刷新逻辑

但现有交互仍存在明显可用性缺口：

- 按钮加载态只体现在按钮自身，持续时间短时不容易被感知
- 请求完成后按钮会立即恢复原文案，用户无法从按钮本身判断是否执行成功
- 成功/失败结果仅写入日志区，用户不盯日志时很难确认动作是否结束

这会导致用户在执行 `更新股票池` 时产生三个典型疑问：

- 是否真的开始执行了
- 当前是否还在执行中
- 最终是否已经更新完成

## 2. Goals And Non-Goals

### 2.1 Goals

- 为 `更新股票池` 增加清晰、持续可见的状态反馈
- 保留现有按钮加载动画，并补充固定状态展示区域
- 在页面内展示 `未开始 / 更新中 / 成功 / 失败` 四种状态
- 在成功或失败后保留最近一次执行结果，便于用户回看
- 保持现有严格草稿执行模式不变
- 尽量不修改后端接口与核心同步逻辑

### 2.2 Non-Goals

- 不改造 `history_sync` 核心执行引擎
- 不新增异步任务队列、轮询任务或后台作业跟踪机制
- 不把股票池更新状态塞进现有 `增量同步运行指标` 区域
- 不在本次统一所有配置中心按钮的状态展示样式
- 不新增数据库持久化的动作状态表

## 3. Scope

本次改动范围限定为：

- `dashboard.html` 中配置中心 `工部增量同步` 区域
- `runHistorySyncStockListRefresh()` 前端交互逻辑
- 前端新增一个轻量级股票池更新状态渲染函数与本地状态对象

本次默认不改动：

- `server.py` 中 `/api/history_sync/stock_list/refresh` 的响应结构
- `src/utils/stock_list_refresh.py`
- `scripts/update_history_sync_stock_list.py`
- `history_sync` 服务状态接口

## 4. Approaches

### 4.1 Recommended Approach: Button Loading State Plus Dedicated Status Panel

保留现有按钮 `spinner` 与禁用态，同时在 `工部增量同步` 区域新增一个独立状态块，专门展示最近一次股票池更新动作的状态与摘要信息。

状态块展示内容包括：

- 当前状态
- 主消息
- 数据源
- 股票数
- 输出路径
- 开始时间
- 完成时间

优点：

- 状态语义清晰，不依赖用户查看日志
- 与现有 `风险提示`、`运行指标` 分工明确
- 改动集中在前端，风险低
- 即使接口执行很快，用户仍能看到最近一次结果

缺点：

- 页面会新增一个小面板

### 4.2 Rejected Approach: Reuse Runtime Metrics Panel

把股票池更新状态复用到现有 `history-sync-runtime-metrics` 区域。

不采用原因：

- 该区域语义是“增量同步运行指标”
- 股票池更新与增量同步属于不同动作，混用后容易引发误解
- 后续扩展时会让渲染逻辑更加混杂

### 4.3 Rejected Approach: Toast And Log Only

继续主要依赖按钮加载态、日志区和浮层提示。

不采用原因：

- 浮层会自动消失，不能作为稳定完成态
- 日志区需要用户主动关注，不符合本次问题的核心诉求
- 无法形成固定的“最近一次执行结果”展示

## 5. Detailed Design

### 5.1 UI Placement

在配置中心 `工部增量同步` 区域中，沿用当前布局，在以下内容之后继续增加一个独立信息块：

- 风险提示
- 运行指标

新增面板建议命名为：

- `股票池更新状态`

该面板与现有信息块同级，避免把不同动作的反馈混在一起。

### 5.2 Frontend State Model

前端新增一个轻量状态对象，例如：

- `historySyncStockListRefreshState`

建议字段如下：

- `status`
- `message`
- `started_at`
- `finished_at`
- `provider`
- `source`
- `codes`
- `output_path`

字段语义：

- `provider` 表示本次请求期望使用的数据源配置，例如 `auto`
- `source` 表示后端最终实际使用的数据源，例如 `akshare` 或 `tushare`
- `codes` 表示最终有效股票数
- `output_path` 表示本次写入路径

初始默认值：

- `status = idle`
- `message = 暂无执行记录`

### 5.3 Status Lifecycle

状态流转定义如下：

1. 初始状态：`idle`
2. 点击按钮并通过草稿校验后：进入 `running`
3. 接口返回成功：进入 `success`
4. 接口返回失败或前端请求异常：进入 `error`

四种状态对应展示语义：

- `idle`：未开始
- `running`：更新中
- `success`：更新成功
- `error`：更新失败

### 5.4 Rendering Strategy

前端新增一个独立渲染函数，例如：

- `renderHistorySyncStockListRefreshStatus()`

职责限定为：

- 根据本地状态对象生成面板文案
- 根据状态切换颜色样式
- 保持无副作用，不直接发请求

展示建议：

- `running` 使用青色或蓝色风格
- `success` 使用绿色风格
- `error` 使用红色风格
- `idle` 使用灰色风格

面板内容建议为四行：

- 第一行：当前状态 + 主消息
- 第二行：数据源、股票数
- 第三行：输出路径
- 第四行：开始时间、完成时间

若某字段为空，则展示 `--`，避免出现空白区域或 `undefined`。

### 5.5 Integration With Existing Button Flow

`runHistorySyncStockListRefresh()` 保持现有严格草稿执行流程，只补充状态写入与渲染调用。

建议流程如下：

1. 读取按钮 DOM
2. 读取当前草稿配置
3. 做必填项校验
4. 构造请求 payload
5. 在发请求前写入 `running` 状态，并立即渲染状态面板
6. 发起 `/api/history_sync/stock_list/refresh` 请求
7. 根据结果更新为 `success` 或 `error`
8. 在 `finally` 中恢复按钮样式

成功时应写入：

- `status=success`
- `message=股票池更新完成`
- `source`
- `codes`
- `output_path`
- `finished_at`

失败时应写入：

- `status=error`
- `message=具体错误信息`
- `finished_at`

如果前端校验阶段就失败，也应把状态面板更新为失败态，而不是只写日志。

### 5.6 Relationship With Logs

现有日志行为保留，不替代：

- 开始时仍写 `使用当前草稿执行股票池更新`
- 成功时仍写完成摘要
- 失败时仍写失败原因

固定状态面板承担“当前动作是否完成、最近一次结果是什么”的职责。

日志区承担“操作流水与排查上下文”的职责。

两者职责明确分离，不互相替代。

## 6. Data Flow

完整流程如下：

1. 用户点击 `更新股票池`
2. 前端读取当前未保存草稿并做必填校验
3. 前端把状态面板切换为 `更新中`
4. 前端调用 `/api/history_sync/stock_list/refresh`
5. 后端执行股票池刷新并返回 `status/msg/result`
6. 前端根据响应结果更新状态面板为 `成功` 或 `失败`
7. 用户无需查看日志，也能在配置中心直接确认结果

## 7. Error Handling

- 草稿缺少必填项时，前端直接拦截，并把状态面板更新为失败态
- 接口返回 `status != success` 时，状态面板显示失败消息
- 网络异常、JSON 解析异常或其他运行时异常时，状态面板统一进入失败态
- 请求进行期间按钮保持禁用，防止重复提交
- 不引入额外轮询；本次动作以单次同步请求为边界

## 8. Testing Strategy

本次以高价值手动验证为主，必要时补充前端函数级回归检查。

至少覆盖以下场景：

- 正常更新成功，状态面板显示成功摘要
- AkShare 失败但 TuShare 兜底成功，状态面板显示最终实际数据源
- 草稿缺失必填项，状态面板显示失败信息，且不发请求
- 后端返回错误时，状态面板进入失败态
- 请求期间按钮禁用且显示加载文案，请求结束后恢复

回归检查重点：

- 不影响现有 `增量同步`、`停止同步`、`刷新状态`
- 不影响严格草稿执行模式
- 不影响现有日志输出

## 9. Risks

- 如果后端返回字段不稳定，前端需要对 `result` 做保守兜底，避免显示 `undefined`
- 如果按钮点击后瞬时成功，按钮动画仍可能不明显，因此状态面板必须承担主要反馈职责
- 若后续更多按钮需要类似状态面板，应避免把本次实现直接抽象成过度通用框架

## 10. Acceptance Criteria

满足以下条件则视为完成：

- 配置中心 `工部增量同步` 区域存在独立的 `股票池更新状态` 面板
- 点击 `更新股票池` 后，页面可见 `更新中` 状态
- 成功后，页面可见 `更新成功`、数据源、股票数、输出路径与时间信息
- 失败后，页面可见 `更新失败` 与错误原因
- 状态结果在请求结束后不会立即消失
- 不需要查看日志，也能判断更新是否完成
- 不改动 `history_sync` 核心逻辑与股票池刷新核心逻辑
