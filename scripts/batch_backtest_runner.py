import argparse
import csv
import json
import multiprocessing
import queue
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


任务列定义 = [
    ("任务ID", "task_id"),
    ("批次号", "batch_no"),
    ("优先级", "priority"),
    ("是否启用", "enabled"),
    ("股票代码", "stock_code"),
    ("策略ID", "strategy_id"),
    ("开始日期", "start_date"),
    ("结束日期", "end_date"),
    ("初始资金", "capital"),
    ("K线周期", "kline_type"),
    ("数据源", "data_source"),
    ("场景标签", "scenario_tag"),
    ("成本档位", "cost_profile"),
    ("滑点BP", "slippage_bp"),
    ("佣金费率", "commission_rate"),
    ("印花税率", "stamp_tax_rate"),
    ("最小手数", "min_lot"),
    ("是否T1", "enforce_t1"),
    ("最大重试", "max_retry"),
    ("任务状态", "status"),
    ("报告ID", "report_id"),
    ("错误信息", "error_msg"),
    ("创建时间", "created_at"),
    ("更新时间", "updated_at"),
]

结果列定义 = [
    ("任务ID", "task_id"),
    ("批次号", "batch_no"),
    ("股票代码", "stock_code"),
    ("策略ID", "strategy_id"),
    ("场景标签", "scenario_tag"),
    ("报告ID", "report_id"),
    ("运行状态", "run_status"),
    ("交易回合数", "total_trades"),
    ("总收益率", "total_return"),
    ("年化收益率", "annualized_return"),
    ("最大回撤", "max_drawdown"),
    ("胜率", "win_rate"),
    ("盈亏比", "profit_factor"),
    ("卡玛比", "calmar"),
    ("夏普比", "sharpe"),
    ("违规次数", "violations"),
    ("拒单次数", "rejections"),
    ("熔断次数", "circuit_breaks"),
    ("原始分", "score_raw"),
    ("惩罚分", "score_penalty"),
    ("最终分", "score_final"),
    ("评级", "grade"),
    ("建议动作", "decision"),
]

汇总列定义 = [
    ("策略ID", "strategy_id"),
    ("任务总数", "task_count"),
    ("成功数", "success_count"),
    ("中位最终分", "median_score_final"),
    ("中位年化收益", "median_annualized_return"),
    ("中位最大回撤", "median_max_drawdown"),
    ("中位胜率", "median_win_rate"),
    ("评级", "grade"),
    ("建议动作", "decision"),
]

任务英文别名 = [
    "task_id",
    "batch_no",
    "priority",
    "enabled",
    "stock_code",
    "strategy_id",
    "start_date",
    "end_date",
    "capital",
    "kline_type",
    "data_source",
    "scenario_tag",
    "cost_profile",
    "slippage_bp",
    "commission_rate",
    "stamp_tax_rate",
    "min_lot",
    "enforce_t1",
    "max_retry",
    "status",
    "report_id",
    "error_msg",
    "created_at",
    "updated_at",
]

结果英文别名 = [
    "task_id",
    "batch_no",
    "stock_code",
    "strategy_id",
    "scenario_tag",
    "report_id",
    "run_status",
    "total_trades",
    "total_return",
    "annualized_return",
    "max_drawdown",
    "win_rate",
    "profit_factor",
    "calmar",
    "sharpe",
    "violations",
    "rejections",
    "circuit_breaks",
    "score_raw",
    "score_penalty",
    "score_final",
    "grade",
    "decision",
]

汇总英文别名 = [
    "strategy_id",
    "task_count",
    "success_count",
    "median_score_final",
    "median_annualized_return",
    "median_max_drawdown",
    "median_win_rate",
    "grade",
    "decision",
]

策略池列定义 = [
    ("策略ID", "strategy_id"),
    ("是否启用", "enabled"),
]

标的池列定义 = [
    ("股票代码", "stock_code"),
    ("市场标签", "market_tag"),
    ("行业标签", "industry_tag"),
    ("市值标签", "size_tag"),
    ("是否启用", "enabled"),
]

区间池列定义 = [
    ("区间ID", "window_id"),
    ("开始日期", "start_date"),
    ("结束日期", "end_date"),
    ("行情标签", "market_phase"),
    ("批次号", "batch_no"),
    ("是否启用", "enabled"),
]

场景池列定义 = [
    ("场景标签", "scenario_tag"),
    ("成本档位", "cost_profile"),
    ("滑点BP", "slippage_bp"),
    ("佣金费率", "commission_rate"),
    ("印花税率", "stamp_tax_rate"),
    ("最小手数", "min_lot"),
    ("是否T1", "enforce_t1"),
    ("K线周期", "kline_type"),
    ("数据源", "data_source"),
    ("初始资金", "capital"),
    ("优先级", "priority"),
    ("最大重试", "max_retry"),
    ("是否启用", "enabled"),
]

策略池英文别名 = ["strategy_id", "enabled"]
标的池英文别名 = ["stock_code", "market_tag", "industry_tag", "size_tag", "enabled"]
区间池英文别名 = ["window_id", "start_date", "end_date", "market_phase", "batch_no", "enabled"]
场景池英文别名 = [
    "scenario_tag",
    "cost_profile",
    "slippage_bp",
    "commission_rate",
    "stamp_tax_rate",
    "min_lot",
    "enforce_t1",
    "kline_type",
    "data_source",
    "capital",
    "priority",
    "max_retry",
    "enabled",
]

状态别名 = {
    "pending": "pending",
    "retry": "retry",
    "running": "running",
    "success": "success",
    "failed": "failed",
    "待执行": "pending",
    "待处理": "pending",
    "重试": "retry",
    "运行中": "running",
    "成功": "success",
    "失败": "failed",
}


def 当前时间() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def 转浮点(v: Any, 默认值: float = 0.0) -> float:
    try:
        if v is None or v == "":
            return 默认值
        return float(v)
    except Exception:
        return 默认值


def 转整数(v: Any, 默认值: int = 0) -> int:
    try:
        if v is None or v == "":
            return 默认值
        return int(float(v))
    except Exception:
        return 默认值


def 截断01(v: float) -> float:
    if v < 0:
        return 0.0
    if v > 1:
        return 1.0
    return v


def 解析布尔(v: str) -> bool:
    return str(v).strip().lower() in {"1", "true", "yes", "y", "是", "启用"}


def 归一状态(v: str) -> str:
    raw = str(v or "").strip().lower()
    return 状态别名.get(raw, raw)


def 规范日期(v: str) -> Tuple[bool, str]:
    raw = str(v or "").strip()
    if not raw:
        return False, ""
    text = raw.replace(".", "-").replace("/", "-")
    for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.strptime(text, fmt)
            return True, dt.strftime("%Y-%m-%d")
        except Exception:
            continue
    return False, raw


def 构建映射(列定义: List[Tuple[str, str]], 英文别名: List[str]) -> Dict[str, str]:
    映射 = {}
    for idx, (中文, 内部) in enumerate(列定义):
        映射[中文] = 内部
        if idx < len(英文别名):
            映射[英文别名[idx]] = 内部
    return 映射


def 标准化行(raw: Dict[str, Any], 列定义: List[Tuple[str, str]], 英文别名: List[str]) -> Dict[str, Any]:
    映射 = 构建映射(列定义, 英文别名)
    out: Dict[str, Any] = {内部: "" for _, 内部 in 列定义}
    for k, v in raw.items():
        key = 映射.get(str(k).strip())
        if key:
            out[key] = v
    return out


def 读取CSV(path: Path, 列定义: List[Tuple[str, str]], 英文别名: List[str]) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.DictReader(f))
    return [标准化行(r, 列定义, 英文别名) for r in rows]


def 写入CSV(path: Path, 列定义: List[Tuple[str, str]], rows: List[Dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    header_cn = [x[0] for x in 列定义]
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header_cn, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            payload = {中文: row.get(内部, "") for 中文, 内部 in 列定义}
            w.writerow(payload)


def 追加CSV(path: Path, 列定义: List[Tuple[str, str]], row: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = path.exists()
    header_cn = [x[0] for x in 列定义]
    with path.open("a", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=header_cn, extrasaction="ignore")
        if not exists:
            w.writeheader()
        payload = {中文: row.get(内部, "") for 中文, 内部 in 列定义}
        w.writerow(payload)


def http_json(base_url: str, method: str, path: str, body: Dict[str, Any] = None, timeout: int = 30) -> Dict[str, Any]:
    url = f"{base_url.rstrip('/')}{path}"
    data = None
    headers = {"Content-Type": "application/json"}
    if body is not None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = Request(url=url, method=method.upper(), headers=headers, data=data)
    with urlopen(req, timeout=timeout) as resp:
        text = resp.read().decode("utf-8")
        if not text:
            return {}
        return json.loads(text)


def 安全请求(base_url: str, method: str, path: str, body: Dict[str, Any] = None, timeout: int = 30) -> Tuple[bool, Dict[str, Any], str]:
    try:
        payload = http_json(base_url, method, path, body=body, timeout=timeout)
        return True, payload, ""
    except HTTPError as e:
        try:
            detail = e.read().decode("utf-8")
        except Exception:
            detail = str(e)
        return False, {}, f"HTTPError {e.code}: {detail}"
    except URLError as e:
        return False, {}, f"URLError: {e}"
    except Exception as e:
        return False, {}, str(e)


def 待执行任务(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    out = []
    for r in rows:
        if not 解析布尔(r.get("enabled", "1")):
            continue
        st = 归一状态(r.get("status", "pending"))
        if st in {"pending", "retry"}:
            out.append(r)
    out.sort(key=lambda x: (str(x.get("batch_no", "")), 转整数(x.get("priority", 99), 99), str(x.get("task_id", ""))))
    return out


def 等待完成(base_url: str, report_id: str, poll_seconds: int, status_log_seconds: int, max_wait_seconds: int) -> Tuple[bool, Dict[str, Any], str]:
    start_ts = time.time()
    next_log = start_ts
    while True:
        ok, st, err = 安全请求(base_url, "GET", "/api/status")
        if not ok:
            return False, {}, f"查询状态失败: {err}"
        is_running = bool(st.get("is_running"))
        current_status = str(st.get("current_report_status") or "")
        current_report_id = str(st.get("current_report_id") or "")
        if current_status.lower() == "failed" and (not current_report_id or current_report_id == str(report_id)):
            return False, st, str(st.get("current_report_error") or "回测执行失败")
        if not is_running:
            return True, st, ""
        now_ts = time.time()
        if now_ts >= next_log:
            progress = st.get("progress", {}) if isinstance(st.get("progress"), dict) else {}
            p = progress.get("progress")
            d = progress.get("current_date")
            print(f"[运行中] report_id={report_id} progress={p} current_date={d}")
            next_log = now_ts + status_log_seconds
        if now_ts - start_ts > max_wait_seconds:
            return False, st, "等待超时"
        time.sleep(poll_seconds)


def 拉取报告(base_url: str, report_id: str) -> Tuple[bool, Dict[str, Any], str]:
    ok, data, err = 安全请求(base_url, "GET", f"/api/report/{report_id}")
    if ok:
        return True, data, ""
    ok_h, hist, err_h = 安全请求(base_url, "GET", "/api/report/history")
    if ok_h and isinstance(hist.get("reports"), list):
        for item in hist["reports"]:
            if str(item.get("report_id")) == str(report_id):
                ok_d, data_d, err_d = 安全请求(base_url, "GET", f"/api/report/{report_id}")
                if ok_d:
                    return True, data_d, ""
                return False, {}, f"report_detail失败: {err_d}"
    ok_l, latest, err_l = 安全请求(base_url, "GET", "/api/report/latest")
    if ok_l:
        rid = str(latest.get("report_id") or "")
        if rid and rid == str(report_id):
            return True, latest, ""
    return False, {}, f"获取报告失败: detail={err}; history={err_h}; latest={err_l}"


def 选取指标(report: Dict[str, Any], strategy_id: str) -> Dict[str, Any]:
    ranking = report.get("ranking")
    if isinstance(ranking, list):
        for row in ranking:
            if str(row.get("strategy_id")) == str(strategy_id):
                return row
        if ranking:
            return ranking[0]
    summary = report.get("summary")
    if isinstance(summary, dict):
        rows = summary.get("ranking")
        if isinstance(rows, list):
            for row in rows:
                if str(row.get("strategy_id")) == str(strategy_id):
                    return row
            if rows:
                return rows[0]
    return {}


def 自动评分(metric: Dict[str, Any]) -> Tuple[float, float, float, str, str]:
    annualized_return = 转浮点(metric.get("annualized_roi", metric.get("annualized_return", 0.0)), 0.0)
    total_return = 转浮点(metric.get("roi", metric.get("total_return", 0.0)), 0.0)
    max_dd = abs(转浮点(metric.get("max_dd", metric.get("max_drawdown", 0.0)), 0.0))
    win_rate = 转浮点(metric.get("win_rate", 0.0), 0.0)
    profit_factor = 转浮点(metric.get("profit_factor", metric.get("profit_ratio", 0.0)), 0.0)
    calmar = 转浮点(metric.get("calmar", 0.0), 0.0)
    sharpe = 转浮点(metric.get("sharpe", 0.0), 0.0)
    violations = 转整数(metric.get("violations", 0), 0)
    rejections = 转整数(metric.get("rejections", 0), 0)
    circuit_breaks = 转整数(metric.get("circuit_breaks", 0), 0)
    s_annual = 截断01((annualized_return - 0.0) / 0.2) * 20
    s_total = 截断01((total_return - 0.0) / 0.3) * 10
    s_calmar = 截断01((calmar - 0.3) / 1.2) * 10
    s_mdd = 截断01((0.3 - max_dd) / 0.3) * 20
    s_sharpe = 截断01((sharpe - 0.2) / 1.5) * 10
    s_rule = 5 if violations == 0 else 0
    s_wr = 截断01((win_rate - 0.35) / 0.3) * 8
    s_pf = 截断01((profit_factor - 1.0) / 1.0) * 7
    score_raw = s_annual + s_total + s_calmar + s_mdd + s_sharpe + s_rule + s_wr + s_pf + 10
    score_penalty = rejections * 0.05 + circuit_breaks * 0.2
    score_final = max(0.0, score_raw - score_penalty)
    if score_final >= 80:
        grade = "A"
        decision = "保留并加权"
    elif score_final >= 65:
        grade = "B"
        decision = "保留观察"
    elif score_final >= 50:
        grade = "C"
        decision = "小仓或重构"
    else:
        grade = "D"
        decision = "淘汰"
    return round(score_raw, 4), round(score_penalty, 4), round(score_final, 4), grade, decision


def 中位数(nums: List[float]) -> float:
    if not nums:
        return 0.0
    s = sorted(nums)
    n = len(s)
    m = n // 2
    if n % 2 == 1:
        return s[m]
    return (s[m - 1] + s[m]) / 2.0


def 按策略汇总(results_rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    bucket: Dict[str, List[Dict[str, Any]]] = {}
    for r in results_rows:
        sid = str(r.get("strategy_id", ""))
        bucket.setdefault(sid, []).append(r)
    out = []
    for sid, rows in bucket.items():
        succ = [x for x in rows if str(x.get("run_status", "")).lower() == "success"]
        score_vals = [转浮点(x.get("score_final"), 0.0) for x in succ]
        annual_vals = [转浮点(x.get("annualized_return"), 0.0) for x in succ]
        mdd_vals = [abs(转浮点(x.get("max_drawdown"), 0.0)) for x in succ]
        wr_vals = [转浮点(x.get("win_rate"), 0.0) for x in succ]
        m_score = 中位数(score_vals)
        if m_score >= 80:
            grade = "A"
            decision = "保留并加权"
        elif m_score >= 65:
            grade = "B"
            decision = "保留观察"
        elif m_score >= 50:
            grade = "C"
            decision = "小仓或重构"
        else:
            grade = "D"
            decision = "淘汰"
        out.append(
            {
                "strategy_id": sid,
                "task_count": len(rows),
                "success_count": len(succ),
                "median_score_final": round(m_score, 4),
                "median_annualized_return": round(中位数(annual_vals), 6),
                "median_max_drawdown": round(中位数(mdd_vals), 6),
                "median_win_rate": round(中位数(wr_vals), 6),
                "grade": grade,
                "decision": decision,
            }
        )
    out.sort(key=lambda x: x["median_score_final"], reverse=True)
    return out


def 单任务执行(task: Dict[str, Any], base_url: str, poll_seconds: int, status_log_seconds: int, max_wait_seconds: int, retry_sleep_seconds: int) -> Dict[str, Any]:
    task_id = str(task.get("task_id", ""))
    stock_code = str(task.get("stock_code", "")).strip()
    strategy_id = str(task.get("strategy_id", "all")).strip() or "all"
    start_date = str(task.get("start_date", "")).strip()
    end_date = str(task.get("end_date", "")).strip()
    capital = 转浮点(task.get("capital", 1_000_000), 1_000_000)
    max_retry = max(0, 转整数(task.get("max_retry", 1), 1))
    scenario_tag = str(task.get("scenario_tag", "")).strip()
    if str(task.get("data_source", "default")).strip().lower() not in {"", "default"}:
        return {
            "task_id": task_id,
            "ok": False,
            "status": "failed",
            "report_id": "",
            "error_msg": "数据源必须为default",
            "result_row": None,
        }
    ok_start, start_date_norm = 规范日期(start_date)
    ok_end, end_date_norm = 规范日期(end_date)
    if not ok_start or not ok_end:
        return {
            "task_id": task_id,
            "ok": False,
            "status": "failed",
            "report_id": "",
            "error_msg": f"日期格式不合法，要求YYYY-MM-DD，当前 start={start_date} end={end_date}",
            "result_row": None,
        }
    start_date = start_date_norm
    end_date = end_date_norm
    attempts = 0
    while attempts <= max_retry:
        attempts += 1
        payload = {
            "stock_code": stock_code,
            "strategy_id": strategy_id,
            "start": start_date,
            "end": end_date,
            "capital": capital,
        }
        print(f"[START] task={task_id} attempt={attempts} base_url={base_url} payload={payload}")
        ok, start_resp, start_err = 安全请求(base_url, "POST", "/api/control/start_backtest", body=payload, timeout=30)
        if not ok or str(start_resp.get("status", "")).lower() != "success":
            if attempts <= max_retry:
                安全请求(base_url, "POST", "/api/control/stop", body=None, timeout=15)
                print(f"[RETRY] task={task_id} 启动失败，准备重试")
                time.sleep(retry_sleep_seconds)
                continue
            return {
                "task_id": task_id,
                "ok": False,
                "status": "failed",
                "report_id": "",
                "error_msg": start_err or str(start_resp.get("msg", "start_backtest失败")),
                "result_row": None,
            }
        report_id = str(start_resp.get("report_id", ""))
        ok_wait, _, wait_err = 等待完成(base_url, report_id, poll_seconds, status_log_seconds, max_wait_seconds)
        if not ok_wait:
            if attempts <= max_retry:
                安全请求(base_url, "POST", "/api/control/stop", body=None, timeout=15)
                print(f"[RETRY] task={task_id} 等待失败，准备重试")
                time.sleep(retry_sleep_seconds)
                continue
            return {
                "task_id": task_id,
                "ok": False,
                "status": "failed",
                "report_id": report_id,
                "error_msg": wait_err,
                "result_row": None,
            }
        ok_report, report, report_err = 拉取报告(base_url, report_id)
        if not ok_report:
            if attempts <= max_retry:
                print(f"[RETRY] task={task_id} 拉取报告失败，准备重试")
                time.sleep(retry_sleep_seconds)
                continue
            return {
                "task_id": task_id,
                "ok": False,
                "status": "failed",
                "report_id": report_id,
                "error_msg": report_err,
                "result_row": None,
            }
        metric = 选取指标(report, strategy_id)
        score_raw, score_penalty, score_final, grade, decision = 自动评分(metric)
        result_row = {
            "task_id": task_id,
            "batch_no": task.get("batch_no", ""),
            "stock_code": stock_code,
            "strategy_id": strategy_id,
            "scenario_tag": scenario_tag,
            "report_id": report_id,
            "run_status": "success",
            "total_trades": 转整数(metric.get("total_trades", metric.get("trade_count", 0)), 0),
            "total_return": 转浮点(metric.get("roi", metric.get("total_return", 0.0)), 0.0),
            "annualized_return": 转浮点(metric.get("annualized_roi", metric.get("annualized_return", 0.0)), 0.0),
            "max_drawdown": abs(转浮点(metric.get("max_dd", metric.get("max_drawdown", 0.0)), 0.0)),
            "win_rate": 转浮点(metric.get("win_rate", 0.0), 0.0),
            "profit_factor": 转浮点(metric.get("profit_factor", metric.get("profit_ratio", 0.0)), 0.0),
            "calmar": 转浮点(metric.get("calmar", 0.0), 0.0),
            "sharpe": 转浮点(metric.get("sharpe", 0.0), 0.0),
            "violations": 转整数(metric.get("violations", 0), 0),
            "rejections": 转整数(metric.get("rejections", 0), 0),
            "circuit_breaks": 转整数(metric.get("circuit_breaks", 0), 0),
            "score_raw": score_raw,
            "score_penalty": score_penalty,
            "score_final": score_final,
            "grade": grade,
            "decision": decision,
        }
        return {
            "task_id": task_id,
            "ok": True,
            "status": "success",
            "report_id": report_id,
            "error_msg": "",
            "result_row": result_row,
        }
    return {
        "task_id": task_id,
        "ok": False,
        "status": "failed",
        "report_id": "",
        "error_msg": "未知错误",
        "result_row": None,
    }


def worker_loop(worker_id: int, base_url: str, in_q: multiprocessing.Queue, out_q: multiprocessing.Queue, poll_seconds: int, status_log_seconds: int, max_wait_seconds: int, retry_sleep_seconds: int, 限流间隔秒: float) -> None:
    last_start = 0.0
    while True:
        try:
            task = in_q.get(timeout=1)
        except queue.Empty:
            continue
        if task is None:
            break
        if 限流间隔秒 > 0 and last_start > 0:
            wait_s = 限流间隔秒 - (time.time() - last_start)
            if wait_s > 0:
                time.sleep(wait_s)
        last_start = time.time()
        result = 单任务执行(
            task=task,
            base_url=base_url,
            poll_seconds=poll_seconds,
            status_log_seconds=status_log_seconds,
            max_wait_seconds=max_wait_seconds,
            retry_sleep_seconds=retry_sleep_seconds,
        )
        result["worker_id"] = worker_id
        result["base_url"] = base_url
        out_q.put(result)


def 初始化模板(path: Path) -> None:
    ts = 当前时间()
    rows = [
        {
            "task_id": "T000001",
            "batch_no": "B01",
            "priority": "1",
            "enabled": "1",
            "stock_code": "600941.SH",
            "strategy_id": "34R1U500V31B",
            "start_date": "2025-07-01",
            "end_date": "2025-12-31",
            "capital": "1000000",
            "kline_type": "5min",
            "data_source": "default",
            "scenario_tag": "base_normal",
            "cost_profile": "cost_base",
            "slippage_bp": "2",
            "commission_rate": "0.00025",
            "stamp_tax_rate": "0.001",
            "min_lot": "100",
            "enforce_t1": "1",
            "max_retry": "1",
            "status": "pending",
            "report_id": "",
            "error_msg": "",
            "created_at": ts,
            "updated_at": ts,
        },
        {
            "task_id": "T000002",
            "batch_no": "B01",
            "priority": "1",
            "enabled": "1",
            "stock_code": "000001.SZ",
            "strategy_id": "34R1U500V31B",
            "start_date": "2025-07-01",
            "end_date": "2025-12-31",
            "capital": "1000000",
            "kline_type": "5min",
            "data_source": "default",
            "scenario_tag": "stress_cost",
            "cost_profile": "cost_x2",
            "slippage_bp": "4",
            "commission_rate": "0.0005",
            "stamp_tax_rate": "0.001",
            "min_lot": "100",
            "enforce_t1": "1",
            "max_retry": "1",
            "status": "pending",
            "report_id": "",
            "error_msg": "",
            "created_at": ts,
            "updated_at": ts,
        },
    ]
    写入CSV(path, 任务列定义, rows)


def 初始化任务生成模板(策略池路径: Path, 标的池路径: Path, 区间池路径: Path, 场景池路径: Path) -> None:
    写入CSV(
        策略池路径,
        策略池列定义,
        [
            {"strategy_id": "34R1U500V31B", "enabled": "1"},
            {"strategy_id": "all", "enabled": "0"},
        ],
    )
    写入CSV(
        标的池路径,
        标的池列定义,
        [
            {"stock_code": "600941.SH", "market_tag": "主板", "industry_tag": "通信", "size_tag": "大盘", "enabled": "1"},
            {"stock_code": "000001.SZ", "market_tag": "主板", "industry_tag": "金融", "size_tag": "大盘", "enabled": "1"},
            {"stock_code": "300750.SZ", "market_tag": "创业板", "industry_tag": "新能源", "size_tag": "大盘", "enabled": "1"},
        ],
    )
    写入CSV(
        区间池路径,
        区间池列定义,
        [
            {"window_id": "W_BULL_1", "start_date": "2020-07-01", "end_date": "2021-02-28", "market_phase": "牛市", "batch_no": "BULL", "enabled": "1"},
            {"window_id": "W_BEAR_1", "start_date": "2021-02-28", "end_date": "2022-10-31", "market_phase": "熊市", "batch_no": "BEAR", "enabled": "1"},
            {"window_id": "W_SIDE_1", "start_date": "2023-01-01", "end_date": "2023-12-31", "market_phase": "震荡", "batch_no": "SIDE", "enabled": "1"},
        ],
    )
    写入CSV(
        场景池路径,
        场景池列定义,
        [
            {
                "scenario_tag": "base_normal",
                "cost_profile": "cost_base",
                "slippage_bp": "2",
                "commission_rate": "0.00025",
                "stamp_tax_rate": "0.001",
                "min_lot": "100",
                "enforce_t1": "1",
                "kline_type": "5min",
                "data_source": "default",
                "capital": "1000000",
                "priority": "1",
                "max_retry": "1",
                "enabled": "1",
            },
            {
                "scenario_tag": "stress_cost_x2",
                "cost_profile": "cost_x2",
                "slippage_bp": "4",
                "commission_rate": "0.0005",
                "stamp_tax_rate": "0.001",
                "min_lot": "100",
                "enforce_t1": "1",
                "kline_type": "5min",
                "data_source": "default",
                "capital": "1000000",
                "priority": "2",
                "max_retry": "1",
                "enabled": "1",
            },
        ],
    )


def 任务唯一键(row: Dict[str, Any]) -> Tuple[str, str, str, str, str, str]:
    return (
        str(row.get("strategy_id", "")).strip(),
        str(row.get("stock_code", "")).strip(),
        str(row.get("start_date", "")).strip(),
        str(row.get("end_date", "")).strip(),
        str(row.get("scenario_tag", "")).strip(),
        str(row.get("kline_type", "")).strip(),
    )


def 提取任务序号(task_id: str) -> int:
    text = "".join([c for c in str(task_id or "") if c.isdigit()])
    if not text:
        return 0
    return 转整数(text, 0)


def 新任务ID(seq: int) -> str:
    return f"T{seq:06d}"


def 生成任务列表(
    现有任务: List[Dict[str, Any]],
    策略池: List[Dict[str, Any]],
    标的池: List[Dict[str, Any]],
    区间池: List[Dict[str, Any]],
    场景池: List[Dict[str, Any]],
    生成模式: str,
    最大生成数: int,
) -> Tuple[List[Dict[str, Any]], int, int]:
    策略有效 = [x for x in 策略池 if 解析布尔(x.get("enabled", "1")) and str(x.get("strategy_id", "")).strip()]
    标的有效 = [x for x in 标的池 if 解析布尔(x.get("enabled", "1")) and str(x.get("stock_code", "")).strip()]
    区间有效 = [
        x
        for x in 区间池
        if 解析布尔(x.get("enabled", "1"))
        and str(x.get("start_date", "")).strip()
        and str(x.get("end_date", "")).strip()
    ]
    场景有效 = [x for x in 场景池 if 解析布尔(x.get("enabled", "1"))]
    if not 策略有效 or not 标的有效 or not 区间有效 or not 场景有效:
        return 现有任务, 0, 0
    if str(生成模式).strip().lower() == "replace":
        base_rows: List[Dict[str, Any]] = []
    else:
        base_rows = list(现有任务)
    已有键 = set([任务唯一键(x) for x in base_rows])
    下一个序号 = max([提取任务序号(x.get("task_id", "")) for x in base_rows] + [0]) + 1
    新增 = 0
    去重跳过 = 0
    ts = 当前时间()
    for 策略 in 策略有效:
        sid = str(策略.get("strategy_id", "")).strip()
        for 标的 in 标的有效:
            code = str(标的.get("stock_code", "")).strip()
            for 区间 in 区间有效:
                start_date = str(区间.get("start_date", "")).strip()
                end_date = str(区间.get("end_date", "")).strip()
                phase = str(区间.get("market_phase", "")).strip()
                batch_no = str(区间.get("batch_no", "")).strip() or str(区间.get("window_id", "")).strip() or "GEN"
                for 场景 in 场景有效:
                    scene_tag = str(场景.get("scenario_tag", "")).strip() or "base"
                    mix_tag = f"{phase}_{scene_tag}" if phase else scene_tag
                    row = {
                        "task_id": 新任务ID(下一个序号),
                        "batch_no": batch_no,
                        "priority": str(转整数(场景.get("priority", 1), 1)),
                        "enabled": "1",
                        "stock_code": code,
                        "strategy_id": sid,
                        "start_date": start_date,
                        "end_date": end_date,
                        "capital": str(转浮点(场景.get("capital", 1000000), 1000000)),
                        "kline_type": str(场景.get("kline_type", "5min")).strip() or "5min",
                        "data_source": str(场景.get("data_source", "default")).strip() or "default",
                        "scenario_tag": mix_tag,
                        "cost_profile": str(场景.get("cost_profile", "cost_base")).strip() or "cost_base",
                        "slippage_bp": str(场景.get("slippage_bp", "2")),
                        "commission_rate": str(场景.get("commission_rate", "0.00025")),
                        "stamp_tax_rate": str(场景.get("stamp_tax_rate", "0.001")),
                        "min_lot": str(场景.get("min_lot", "100")),
                        "enforce_t1": "1" if 解析布尔(场景.get("enforce_t1", "1")) else "0",
                        "max_retry": str(转整数(场景.get("max_retry", 1), 1)),
                        "status": "pending",
                        "report_id": "",
                        "error_msg": "",
                        "created_at": ts,
                        "updated_at": ts,
                    }
                    key = 任务唯一键(row)
                    if key in 已有键:
                        去重跳过 += 1
                        continue
                    base_rows.append(row)
                    已有键.add(key)
                    新增 += 1
                    下一个序号 += 1
                    if 最大生成数 > 0 and 新增 >= 最大生成数:
                        return base_rows, 新增, 去重跳过
    return base_rows, 新增, 去重跳过


def 解析必需行情(text: str) -> List[str]:
    parts = [x.strip() for x in str(text or "").replace("，", ",").split(",") if x.strip()]
    if not parts:
        return ["牛市", "熊市", "震荡"]
    uniq = []
    seen = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return uniq


def 任务是否启用(row: Dict[str, Any]) -> bool:
    return 解析布尔(row.get("enabled", "1"))


def 构建区间标签映射(区间池: List[Dict[str, Any]]) -> Dict[Tuple[str, str], str]:
    out: Dict[Tuple[str, str], str] = {}
    for w in 区间池:
        if not 任务是否启用(w):
            continue
        s = str(w.get("start_date", "")).strip()
        e = str(w.get("end_date", "")).strip()
        p = str(w.get("market_phase", "")).strip()
        if s and e and p:
            out[(s, e)] = p
    return out


def 推断行情标签(task: Dict[str, Any], 区间标签映射: Dict[Tuple[str, str], str], 必需行情: List[str]) -> str:
    scenario_tag = str(task.get("scenario_tag", "")).strip()
    if "_" in scenario_tag:
        prefix = scenario_tag.split("_", 1)[0].strip()
        if prefix in 必需行情:
            return prefix
    s = str(task.get("start_date", "")).strip()
    e = str(task.get("end_date", "")).strip()
    win_phase = 区间标签映射.get((s, e), "")
    if win_phase:
        return win_phase
    for p in 必需行情:
        if p in scenario_tag:
            return p
    return ""


def 覆盖率检查(
    任务列表: List[Dict[str, Any]],
    标的池: List[Dict[str, Any]],
    区间池: List[Dict[str, Any]],
    必需行情: List[str],
) -> Dict[str, Any]:
    启用任务 = [x for x in 任务列表 if 任务是否启用(x)]
    启用标的池 = [x for x in 标的池 if 任务是否启用(x) and str(x.get("stock_code", "")).strip()]
    区间标签映射 = 构建区间标签映射(区间池)
    股票标签映射: Dict[str, Dict[str, str]] = {}
    for s in 启用标的池:
        code = str(s.get("stock_code", "")).strip()
        股票标签映射[code] = {
            "industry_tag": str(s.get("industry_tag", "")).strip(),
            "size_tag": str(s.get("size_tag", "")).strip(),
            "market_tag": str(s.get("market_tag", "")).strip(),
        }
    全部行业 = sorted(set([v.get("industry_tag", "") for v in 股票标签映射.values() if v.get("industry_tag", "")]))
    全部市值 = sorted(set([v.get("size_tag", "") for v in 股票标签映射.values() if v.get("size_tag", "")]))
    已覆盖行业 = set()
    已覆盖市值 = set()
    策略统计: Dict[str, Dict[str, Any]] = {}
    for t in 启用任务:
        sid = str(t.get("strategy_id", "")).strip() or "UNKNOWN"
        code = str(t.get("stock_code", "")).strip()
        phase = 推断行情标签(t, 区间标签映射, 必需行情)
        策略统计.setdefault(
            sid,
            {
                "task_count": 0,
                "phases": set(),
                "industries": set(),
                "sizes": set(),
            },
        )
        策略统计[sid]["task_count"] += 1
        if phase:
            策略统计[sid]["phases"].add(phase)
        tag = 股票标签映射.get(code, {})
        ind = str(tag.get("industry_tag", "")).strip()
        siz = str(tag.get("size_tag", "")).strip()
        if ind:
            策略统计[sid]["industries"].add(ind)
            已覆盖行业.add(ind)
        if siz:
            策略统计[sid]["sizes"].add(siz)
            已覆盖市值.add(siz)
    策略结果 = []
    缺行情策略 = []
    for sid, stat in sorted(策略统计.items(), key=lambda x: x[0]):
        phases = sorted(stat["phases"])
        missing_phases = [p for p in 必需行情 if p not in stat["phases"]]
        if missing_phases:
            缺行情策略.append({"strategy_id": sid, "missing_phases": missing_phases})
        missing_ind = [x for x in 全部行业 if x not in stat["industries"]]
        missing_size = [x for x in 全部市值 if x not in stat["sizes"]]
        策略结果.append(
            {
                "strategy_id": sid,
                "task_count": stat["task_count"],
                "covered_phases": phases,
                "missing_phases": missing_phases,
                "missing_industries": missing_ind,
                "missing_sizes": missing_size,
            }
        )
    全局缺行业 = [x for x in 全部行业 if x not in 已覆盖行业]
    全局缺市值 = [x for x in 全部市值 if x not in 已覆盖市值]
    return {
        "required_phases": 必需行情,
        "summary": {
            "total_tasks": len(任务列表),
            "enabled_tasks": len(启用任务),
            "strategy_count": len(策略统计),
            "stock_pool_count": len(启用标的池),
            "industry_level_count": len(全部行业),
            "size_level_count": len(全部市值),
        },
        "missing_phase_strategies": 缺行情策略,
        "global_missing_industries": 全局缺行业,
        "global_missing_sizes": 全局缺市值,
        "per_strategy": 策略结果,
    }


def 打印覆盖率结果(payload: Dict[str, Any]) -> None:
    summary = payload.get("summary", {})
    print("=== 覆盖率检查结果 ===")
    print(
        f"任务总数={summary.get('total_tasks',0)} 启用任务={summary.get('enabled_tasks',0)} "
        f"策略数={summary.get('strategy_count',0)} 标的池数={summary.get('stock_pool_count',0)}"
    )
    print(f"必需行情: {','.join(payload.get('required_phases', []))}")
    missing_phase_strategies = payload.get("missing_phase_strategies", [])
    if missing_phase_strategies:
        print("缺牛熊震荡样本的策略:")
        for x in missing_phase_strategies:
            print(f"  - {x.get('strategy_id')}: 缺 {','.join(x.get('missing_phases', []))}")
    else:
        print("所有策略均覆盖了必需行情样本。")
    miss_ind = payload.get("global_missing_industries", [])
    miss_size = payload.get("global_missing_sizes", [])
    print(f"全局未覆盖行业层级: {','.join(miss_ind) if miss_ind else '无'}")
    print(f"全局未覆盖市值层级: {','.join(miss_size) if miss_size else '无'}")


def 执行覆盖率硬门禁(payload: Dict[str, Any], args: argparse.Namespace) -> Tuple[bool, List[str]]:
    reasons: List[str] = []
    missing_phase_strategies = payload.get("missing_phase_strategies", [])
    global_missing_industries = payload.get("global_missing_industries", [])
    global_missing_sizes = payload.get("global_missing_sizes", [])

    phase_missing_count = len(missing_phase_strategies)
    industry_missing_count = len(global_missing_industries)
    size_missing_count = len(global_missing_sizes)

    if phase_missing_count > int(args.coverage_max_missing_phase_strategies):
        reasons.append(
            f"缺行情样本策略数={phase_missing_count}，超过阈值={int(args.coverage_max_missing_phase_strategies)}"
        )
    if industry_missing_count > int(args.coverage_max_missing_industries):
        reasons.append(
            f"行业缺口数={industry_missing_count}，超过阈值={int(args.coverage_max_missing_industries)}"
        )
    if size_missing_count > int(args.coverage_max_missing_sizes):
        reasons.append(
            f"市值缺口数={size_missing_count}，超过阈值={int(args.coverage_max_missing_sizes)}"
        )
    return len(reasons) == 0, reasons


def 打印门禁结论(通过: bool, reasons: List[str], exit_code: int) -> None:
    if 通过:
        print("=== 硬门禁结论: 通过 ===")
        return
    print("=== 硬门禁结论: 未通过 ===")
    for r in reasons:
        print(f"  - {r}")
    print(f"将返回非0退出码: {exit_code}")


def 解析服务地址(args: argparse.Namespace) -> List[str]:
    urls = []
    raw = str(args.base_urls or "").strip()
    if raw:
        urls.extend([x.strip() for x in raw.split(",") if x.strip()])
    if not urls:
        urls.append(str(args.base_url).strip())
    uniq = []
    seen = set()
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            uniq.append(u)
    return uniq


def 执行串行(candidates: List[Dict[str, Any]], tasks_rows: List[Dict[str, Any]], tasks_map: Dict[str, Dict[str, Any]], results_cache: List[Dict[str, Any]], args: argparse.Namespace, tasks_path: Path, results_path: Path) -> None:
    base_url = str(args.base_url).strip()
    for task in candidates:
        tid = str(task.get("task_id", ""))
        row = tasks_map[tid]
        row["status"] = "running"
        row["error_msg"] = ""
        row["updated_at"] = 当前时间()
        写入CSV(tasks_path, 任务列定义, tasks_rows)
        result = 单任务执行(
            task=task,
            base_url=base_url,
            poll_seconds=args.poll_seconds,
            status_log_seconds=args.status_log_seconds,
            max_wait_seconds=args.max_wait_seconds,
            retry_sleep_seconds=args.retry_sleep_seconds,
        )
        row["status"] = result.get("status", "failed")
        row["report_id"] = result.get("report_id", "")
        row["error_msg"] = result.get("error_msg", "")
        row["updated_at"] = 当前时间()
        写入CSV(tasks_path, 任务列定义, tasks_rows)
        if result.get("ok") and isinstance(result.get("result_row"), dict):
            追加CSV(results_path, 结果列定义, result["result_row"])
            results_cache.append(result["result_row"])
            print(f"[DONE] task={tid} report_id={result.get('report_id')} score={result['result_row'].get('score_final')} grade={result['result_row'].get('grade')}")
        else:
            print(f"[FAILED] task={tid} err={result.get('error_msg')}")


def 执行并发(candidates: List[Dict[str, Any]], tasks_rows: List[Dict[str, Any]], tasks_map: Dict[str, Dict[str, Any]], results_cache: List[Dict[str, Any]], args: argparse.Namespace, tasks_path: Path, results_path: Path) -> None:
    base_urls = 解析服务地址(args)
    workers = max(1, int(args.parallel_workers))
    if len(base_urls) == 1 and workers > 1:
        workers = 1
        print("仅提供一个服务地址，并发会互相取消任务，已自动降级为串行。")
    workers = min(workers, max(1, len(base_urls)))
    in_q: multiprocessing.Queue = multiprocessing.Queue()
    out_q: multiprocessing.Queue = multiprocessing.Queue()
    proc_list: List[multiprocessing.Process] = []
    for i in range(workers):
        p = multiprocessing.Process(
            target=worker_loop,
            args=(
                i + 1,
                base_urls[i % len(base_urls)],
                in_q,
                out_q,
                args.poll_seconds,
                args.status_log_seconds,
                args.max_wait_seconds,
                args.retry_sleep_seconds,
                float(args.rate_limit_interval_seconds),
            ),
        )
        p.start()
        proc_list.append(p)
    for task in candidates:
        tid = str(task.get("task_id", ""))
        row = tasks_map[tid]
        row["status"] = "running"
        row["error_msg"] = ""
        row["updated_at"] = 当前时间()
        写入CSV(tasks_path, 任务列定义, tasks_rows)
        in_q.put(task)
    for _ in proc_list:
        in_q.put(None)
    remaining = len(candidates)
    while remaining > 0:
        result = out_q.get()
        tid = str(result.get("task_id", ""))
        row = tasks_map[tid]
        row["status"] = result.get("status", "failed")
        row["report_id"] = result.get("report_id", "")
        row["error_msg"] = result.get("error_msg", "")
        row["updated_at"] = 当前时间()
        写入CSV(tasks_path, 任务列定义, tasks_rows)
        if result.get("ok") and isinstance(result.get("result_row"), dict):
            追加CSV(results_path, 结果列定义, result["result_row"])
            results_cache.append(result["result_row"])
            print(
                f"[DONE] worker={result.get('worker_id')} base={result.get('base_url')} "
                f"task={tid} report_id={result.get('report_id')} score={result['result_row'].get('score_final')}"
            )
        else:
            print(f"[FAILED] worker={result.get('worker_id')} task={tid} err={result.get('error_msg')}")
        remaining -= 1
    for p in proc_list:
        p.join()


def run(args: argparse.Namespace) -> int:
    tasks_path = Path(args.tasks_csv).resolve()
    results_path = Path(args.results_csv).resolve()
    summary_path = Path(args.summary_csv).resolve()
    策略池路径 = Path(args.generator_strategies_csv).resolve()
    标的池路径 = Path(args.generator_stocks_csv).resolve()
    区间池路径 = Path(args.generator_windows_csv).resolve()
    场景池路径 = Path(args.generator_scenarios_csv).resolve()
    if args.init_generator_templates:
        初始化任务生成模板(策略池路径, 标的池路径, 区间池路径, 场景池路径)
        print(f"策略池模板: {策略池路径}")
        print(f"标的池模板: {标的池路径}")
        print(f"区间池模板: {区间池路径}")
        print(f"场景池模板: {场景池路径}")
        return 0
    if args.init_template:
        初始化模板(tasks_path)
        print(f"模板已生成: {tasks_path}")
        return 0
    if args.generate_tasks:
        现有任务 = 读取CSV(tasks_path, 任务列定义, 任务英文别名)
        策略池 = 读取CSV(策略池路径, 策略池列定义, 策略池英文别名)
        标的池 = 读取CSV(标的池路径, 标的池列定义, 标的池英文别名)
        区间池 = 读取CSV(区间池路径, 区间池列定义, 区间池英文别名)
        场景池 = 读取CSV(场景池路径, 场景池列定义, 场景池英文别名)
        新任务, 新增数, 去重跳过 = 生成任务列表(
            现有任务=现有任务,
            策略池=策略池,
            标的池=标的池,
            区间池=区间池,
            场景池=场景池,
            生成模式=args.generate_mode,
            最大生成数=args.generate_max_tasks,
        )
        写入CSV(tasks_path, 任务列定义, 新任务)
        print(f"任务文件: {tasks_path}")
        print(f"新增任务数: {新增数}")
        print(f"去重跳过数: {去重跳过}")
        print(f"当前总任务数: {len(新任务)}")
        if not args.run_after_generate:
            return 0
    需要覆盖率检查 = bool(args.coverage_check or args.coverage_hard_gate)
    if 需要覆盖率检查:
        tasks_rows = 读取CSV(tasks_path, 任务列定义, 任务英文别名)
        标的池 = 读取CSV(标的池路径, 标的池列定义, 标的池英文别名)
        区间池 = 读取CSV(区间池路径, 区间池列定义, 区间池英文别名)
        必需行情 = 解析必需行情(args.coverage_required_phases)
        payload = 覆盖率检查(tasks_rows, 标的池, 区间池, 必需行情)
        打印覆盖率结果(payload)
        if args.coverage_output_json:
            out_path = Path(args.coverage_output_json).resolve()
            out_path.parent.mkdir(parents=True, exist_ok=True)
            with out_path.open("w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            print(f"覆盖率明细文件: {out_path}")
        if args.coverage_hard_gate:
            通过, reasons = 执行覆盖率硬门禁(payload, args)
            打印门禁结论(通过, reasons, int(args.coverage_fail_exit_code))
            if not 通过:
                return int(args.coverage_fail_exit_code)
        if args.coverage_check and (not args.run_after_coverage_check):
            return 0
    tasks_rows = 读取CSV(tasks_path, 任务列定义, 任务英文别名)
    if not tasks_rows:
        print(f"未找到任务或任务为空: {tasks_path}")
        return 1
    candidates = 待执行任务(tasks_rows)
    if args.max_tasks > 0:
        candidates = candidates[: args.max_tasks]
    if not candidates:
        print("没有待执行任务")
        return 0
    results_cache = 读取CSV(results_path, 结果列定义, 结果英文别名)
    tasks_map = {str(x.get("task_id", "")): x for x in tasks_rows}
    if args.parallel_workers > 1:
        执行并发(candidates, tasks_rows, tasks_map, results_cache, args, tasks_path, results_path)
    else:
        执行串行(candidates, tasks_rows, tasks_map, results_cache, args, tasks_path, results_path)
    summary_rows = 按策略汇总(results_cache)
    写入CSV(summary_path, 汇总列定义, summary_rows)
    print(f"结果文件: {results_path}")
    print(f"汇总文件: {summary_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="批量回测执行与自动评分脚本（中文字段 + 并发队列 + 任务生成器）")
    p.add_argument("--base-url", default="http://127.0.0.1:8000", help="单服务地址")
    p.add_argument("--base-urls", default="", help="多服务地址，逗号分隔，用于并发")
    p.add_argument("--tasks-csv", default="data/批量回测任务.csv", help="任务CSV路径")
    p.add_argument("--results-csv", default="data/批量回测结果.csv", help="结果CSV路径")
    p.add_argument("--summary-csv", default="data/策略汇总评分.csv", help="策略汇总CSV路径")
    p.add_argument("--init-template", action="store_true", help="生成任务模板CSV后退出")
    p.add_argument("--poll-seconds", type=int, default=3, help="状态轮询间隔秒")
    p.add_argument("--status-log-seconds", type=int, default=90, help="进度打印间隔秒")
    p.add_argument("--max-wait-seconds", type=int, default=7200, help="单任务最长等待秒")
    p.add_argument("--retry-sleep-seconds", type=int, default=3, help="重试前等待秒")
    p.add_argument("--max-tasks", type=int, default=0, help="最多执行N个任务，0表示不限制")
    p.add_argument("--parallel-workers", type=int, default=1, help="并发进程数")
    p.add_argument("--rate-limit-interval-seconds", type=float, default=0.0, help="每个进程启动任务的最小间隔秒")
    p.add_argument("--init-generator-templates", action="store_true", help="生成任务生成器的输入模板后退出")
    p.add_argument("--generator-strategies-csv", default="data/任务生成_策略池.csv", help="策略池CSV路径")
    p.add_argument("--generator-stocks-csv", default="data/任务生成_标的池.csv", help="标的池CSV路径")
    p.add_argument("--generator-windows-csv", default="data/任务生成_区间池.csv", help="区间池CSV路径")
    p.add_argument("--generator-scenarios-csv", default="data/任务生成_场景池.csv", help="场景池CSV路径")
    p.add_argument("--generate-tasks", action="store_true", help="按策略池/标的池/区间池/场景池自动生成任务")
    p.add_argument("--generate-mode", default="append", choices=["append", "replace"], help="任务生成模式")
    p.add_argument("--generate-max-tasks", type=int, default=0, help="生成任务上限，0表示不限制")
    p.add_argument("--run-after-generate", action="store_true", help="生成任务后继续执行回测")
    p.add_argument("--coverage-check", action="store_true", help="执行覆盖率检查（策略行情/行业/市值）")
    p.add_argument("--coverage-required-phases", default="牛市,熊市,震荡", help="覆盖率检查的必需行情标签，逗号分隔")
    p.add_argument("--coverage-output-json", default="data/覆盖率检查结果.json", help="覆盖率检查结果输出JSON路径")
    p.add_argument("--run-after-coverage-check", action="store_true", help="覆盖率检查后继续执行回测")
    p.add_argument("--coverage-hard-gate", action="store_true", help="开启覆盖率硬门禁，不通过直接非0退出并阻止回测")
    p.add_argument("--coverage-max-missing-phase-strategies", type=int, default=0, help="允许缺行情样本的策略数阈值")
    p.add_argument("--coverage-max-missing-industries", type=int, default=0, help="允许全局行业缺口数阈值")
    p.add_argument("--coverage-max-missing-sizes", type=int, default=0, help="允许全局市值缺口数阈值")
    p.add_argument("--coverage-fail-exit-code", type=int, default=2, help="硬门禁不通过时返回码")
    return p


if __name__ == "__main__":
    parser = build_parser()
    args = parser.parse_args()
    raise SystemExit(run(args))
