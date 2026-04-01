import os
import json
import logging
import threading
from datetime import datetime, timedelta
from typing import Any, Optional

import pandas as pd
import requests

from src.utils.config_loader import ConfigLoader
from src.utils.indicators import Indicators
from src.utils.tushare_provider import TushareProvider
from src.utils.mysql_provider import MysqlProvider
from src.utils.postgres_provider import PostgresProvider

logger = logging.getLogger("HistorySyncService")


TABLE_INTERVAL_MAP = {
    "dat_1mins": "1min",
    "dat_5mins": "5min",
    "dat_10mins": "10min",
    "dat_15mins": "15min",
    "dat_30mins": "30min",
    "dat_60mins": "60min",
    "dat_days": "D",
}

DEFAULT_SYNC_TABLES = [
    "dat_1mins",
    "dat_5mins",
    "dat_10mins",
    "dat_15mins",
    "dat_30mins",
    "dat_60mins",
    "dat_days",
]


class HistoryDiffSyncService:
    def __init__(self):
        self._run_lock = threading.Lock()
        self._is_running = False
        self._stop_requested = False
        self._last_report: dict[str, Any] = {}
        self._last_record: dict[str, Any] = {}
        self._records_dir = os.path.join("reports", "history_sync")

    def get_status(self) -> dict[str, Any]:
        return {
            "is_running": self._is_running,
            "stop_requested": self._stop_requested,
            "last_report": self._last_report,
            "last_record": self._last_record,
        }

    def request_stop(self) -> dict[str, Any]:
        if not self._is_running:
            return {"status": "idle", "msg": "no running sync task"}
        self._stop_requested = True
        return {"status": "success", "msg": "stop requested"}

    def run_sync(self, payload: dict[str, Any]) -> dict[str, Any]:
        if not self._run_lock.acquire(blocking=False):
            return {"status": "busy", "msg": "sync task is already running", "report": self._last_report}
        self._is_running = True
        self._stop_requested = False
        started_at = datetime.now()
        normalized_payload = payload or {}
        try:
            report = self._run_sync_impl(normalized_payload)
            report["started_at"] = started_at.isoformat(timespec="seconds")
            report["finished_at"] = datetime.now().isoformat(timespec="seconds")
            self._last_report = report
            result = {"status": "success", "report": report}
            record = self._persist_run_record(payload=normalized_payload, result=result)
            result["record"] = record
            self._last_record = record
            return result
        except RuntimeError as e:
            msg = str(e)
            stopped = "sync stopped by user" in msg
            report = {
                "status": "stopped" if stopped else "failed",
                "error": msg,
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._last_report = report
            result = {"status": "stopped" if stopped else "error", "msg": msg, "report": report}
            record = self._persist_run_record(payload=normalized_payload, result=result)
            result["record"] = record
            self._last_record = record
            return result
        except Exception as e:
            report = {
                "status": "failed",
                "error": str(e),
                "started_at": started_at.isoformat(timespec="seconds"),
                "finished_at": datetime.now().isoformat(timespec="seconds"),
            }
            self._last_report = report
            result = {"status": "error", "msg": str(e), "report": report}
            record = self._persist_run_record(payload=normalized_payload, result=result)
            result["record"] = record
            self._last_record = record
            return result
        finally:
            self._is_running = False
            self._stop_requested = False
            self._run_lock.release()

    def list_records(self, limit: int = 20, offset: int = 0) -> dict[str, Any]:
        self._ensure_records_dir()
        files = sorted(
            [f for f in os.listdir(self._records_dir) if f.startswith("record_") and f.endswith(".json")],
            reverse=True,
        )
        total = len(files)
        start = max(0, int(offset or 0))
        end = max(start, start + max(1, min(int(limit or 20), 200)))
        items: list[dict[str, Any]] = []
        for name in files[start:end]:
            file_path = os.path.join(self._records_dir, name)
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    record = json.load(f)
                items.append(
                    {
                        "run_id": record.get("run_id"),
                        "status": record.get("status"),
                        "created_at": record.get("created_at"),
                        "summary": record.get("summary", {}),
                        "payload": record.get("payload", {}),
                        "detail_csv_path": record.get("detail_csv_path"),
                        "record_path": file_path,
                    }
                )
            except Exception:
                continue
        return {"total": total, "items": items}

    def get_record(self, run_id: str) -> Optional[dict[str, Any]]:
        rid = str(run_id or "").strip()
        if not rid:
            return None
        file_path = os.path.join(self._records_dir, f"record_{rid}.json")
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                record = json.load(f)
            record["record_path"] = file_path
            return record
        except Exception:
            return None

    def _ensure_records_dir(self) -> None:
        os.makedirs(self._records_dir, exist_ok=True)

    def _build_detail_rows(self, report: dict[str, Any]) -> list[dict[str, Any]]:
        detail_rows: list[dict[str, Any]] = []
        code_reports = report.get("code_reports", []) if isinstance(report, dict) else []
        for code_row in code_reports:
            code = str((code_row or {}).get("code", "") or "")
            tables = (code_row or {}).get("tables", [])
            if not isinstance(tables, list):
                continue
            for table_row in tables:
                if not isinstance(table_row, dict):
                    continue
                detail_rows.append(
                    {
                        "code": code,
                        "table": table_row.get("table"),
                        "source_rows": int(table_row.get("source_rows", 0) or 0),
                        "existing_rows": int(table_row.get("existing_rows", 0) or 0),
                        "missing_rows": int(table_row.get("missing_rows", 0) or 0),
                        "written_rows": int(table_row.get("written_rows", 0) or 0),
                    }
                )
        return detail_rows

    def _persist_run_record(self, payload: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        self._ensure_records_dir()
        report = result.get("report", {}) if isinstance(result, dict) else {}
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        detail_rows = self._build_detail_rows(report if isinstance(report, dict) else {})
        detail_csv_path = os.path.join(self._records_dir, f"detail_{run_id}.csv")
        detail_df = pd.DataFrame(detail_rows)
        detail_df.to_csv(detail_csv_path, index=False, encoding="utf-8-sig")
        summary = {
            "codes_total": int((report or {}).get("codes_total", 0) or 0),
            "tables": (report or {}).get("tables", []),
            "total_source_rows": int((report or {}).get("total_source_rows", 0) or 0),
            "total_existing_rows": int((report or {}).get("total_existing_rows", 0) or 0),
            "total_missing_rows": int((report or {}).get("total_missing_rows", 0) or 0),
            "total_written_rows": int((report or {}).get("total_written_rows", 0) or 0),
        }
        record = {
            "run_id": run_id,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "status": result.get("status"),
            "payload": payload,
            "msg": result.get("msg", ""),
            "summary": summary,
            "report": report,
            "detail_csv_path": detail_csv_path,
        }
        record_path = os.path.join(self._records_dir, f"record_{run_id}.json")
        with open(record_path, "w", encoding="utf-8") as f:
            json.dump(record, f, ensure_ascii=False, indent=2)
        return {"run_id": run_id, "record_path": record_path, "detail_csv_path": detail_csv_path}

    def _run_sync_impl(self, payload: dict[str, Any]) -> dict[str, Any]:
        cfg = ConfigLoader.reload()
        write_mode = str(payload.get("write_mode", cfg.get("history_sync.write_mode", "api")) or "api").strip().lower()
        if write_mode not in {"api", "direct_db"}:
            raise RuntimeError("history_sync.write_mode must be one of: api, direct_db")
        direct_db_source = str(payload.get("direct_db_source", cfg.get("history_sync.direct_db_source", "mysql")) or "mysql").strip().lower()
        if direct_db_source not in {"mysql", "postgresql"}:
            raise RuntimeError("history_sync.direct_db_source must be one of: mysql, postgresql")
        history_base_url = str(cfg.get("data_provider.default_api_url", "") or "").strip().rstrip("/")
        history_api_key = str(cfg.get("data_provider.default_api_key", "") or "").strip()
        tushare_token = str(cfg.get("data_provider.tushare_token", "") or "").strip()
        if write_mode == "api":
            if not history_base_url:
                raise RuntimeError("missing data_provider.default_api_url")
            if not history_api_key:
                raise RuntimeError("missing data_provider.default_api_key")
        if not tushare_token:
            raise RuntimeError("missing data_provider.tushare_token")

        lookback_days = int(payload.get("lookback_days", 10) or 10)
        max_codes = int(payload.get("max_codes", 10000) or 10000)
        batch_size = int(payload.get("batch_size", 500) or 500)
        dry_run = self._as_bool(payload.get("dry_run", False), False)
        on_duplicate = str(payload.get("on_duplicate", "ignore") or "ignore")
        time_mode = str(payload.get("time_mode", cfg.get("history_sync.time_mode", "lookback")) or "lookback").strip().lower()
        if time_mode not in {"lookback", "custom"}:
            time_mode = "lookback"
        intraday_mode = self._as_bool(payload.get("intraday_mode", cfg.get("history_sync.intraday_mode", False)), False)
        session_only = self._as_bool(payload.get("session_only", cfg.get("history_sync.session_only", True)), True)
        start_time, end_time = self._resolve_time_range(
            payload=payload,
            cfg=cfg,
            lookback_days=lookback_days,
            time_mode=time_mode,
            intraday_mode=intraday_mode,
        )
        if start_time >= end_time:
            raise RuntimeError("start_time must be earlier than end_time")

        selected_tables = payload.get("tables")
        if not selected_tables:
            tables = list(DEFAULT_SYNC_TABLES)
        else:
            tables = [str(t).strip() for t in selected_tables if str(t).strip() in TABLE_INTERVAL_MAP]
        if not tables:
            raise RuntimeError("no valid tables selected")

        codes = self._resolve_codes(payload.get("codes"), max_codes=max_codes)
        if not codes:
            raise RuntimeError("no stock codes available")

        headers = {"x-api-key": history_api_key, "Content-Type": "application/json"} if write_mode == "api" else {}
        provider = TushareProvider(token=tushare_token)
        session = requests.Session() if write_mode == "api" else None
        target_db_provider = self._build_target_db_provider(write_mode=write_mode, direct_db_source=direct_db_source)
        self._ensure_target_db_ready(
            write_mode=write_mode,
            provider=target_db_provider,
            sample_code=codes[0],
        )

        summary = {
            "codes_total": len(codes),
            "tables": tables,
            "dry_run": dry_run,
            "write_mode": write_mode,
            "direct_db_source": direct_db_source if write_mode == "direct_db" else "",
            "time_mode": time_mode,
            "session_only": session_only,
            "start_time": start_time.isoformat(timespec="seconds"),
            "end_time": end_time.isoformat(timespec="seconds"),
            "total_source_rows": 0,
            "total_existing_rows": 0,
            "total_missing_rows": 0,
            "total_written_rows": 0,
            "code_reports": [],
        }
        logger.info(
            f"history sync run started: codes={len(codes)} tables={tables} write_mode={write_mode} dry_run={dry_run}"
        )

        for code in codes:
            self._check_stop_requested(context=f"before code {code}")
            self._ensure_target_db_ready(
                write_mode=write_mode,
                provider=target_db_provider,
                sample_code=code,
            )
            logger.info(f"history sync processing code={code}")
            source_frames = self._build_source_frames(provider, code, start_time, end_time, tables, session_only=session_only)
            code_report = {"code": code, "tables": []}
            for table in tables:
                self._check_stop_requested(context=f"before table {table} code {code}")
                source_df = source_frames.get(table)
                if source_df is None or source_df.empty:
                    code_report["tables"].append(
                        {
                            "table": table,
                            "source_rows": 0,
                            "existing_rows": 0,
                            "missing_rows": 0,
                            "written_rows": 0,
                        }
                    )
                    continue
                key_col = "trade_time" if table != "dat_days" else "date"
                if write_mode == "api":
                    existing_keys = self._fetch_existing_keys(
                        session=session,
                        base_url=history_base_url,
                        headers=headers,
                        table=table,
                        code=code,
                        start_time=start_time,
                        end_time=end_time,
                    )
                else:
                    existing_keys = self._fetch_existing_keys_from_db(
                        provider=target_db_provider,
                        table=table,
                        code=code,
                        start_time=start_time,
                        end_time=end_time,
                    )
                source_keys = source_df[key_col].map(lambda x: self._normalize_time_key(x, is_day=(table == "dat_days")))
                missing_mask = ~source_keys.isin(existing_keys)
                missing_df = source_df.loc[missing_mask].copy()
                written_rows = 0
                if not missing_df.empty:
                    preview = self._build_write_preview(table=table, df=missing_df)
                    logger.info(
                        f"history sync write preview code={code} table={table} mode={write_mode} "
                        f"rows={len(missing_df)} preview={preview}"
                    )
                if not dry_run and not missing_df.empty:
                    if write_mode == "api":
                        rows = missing_df.to_dict("records")
                        written_rows = self._push_rows(
                            session=session,
                            base_url=history_base_url,
                            headers=headers,
                            table=table,
                            rows=rows,
                            batch_size=batch_size,
                            on_duplicate=on_duplicate,
                        )
                    else:
                        upsert_df = self._build_direct_db_upsert_df(table=table, df=missing_df)
                        interval = TABLE_INTERVAL_MAP.get(table, "1min")
                        written_rows = int(target_db_provider.upsert_kline_data(upsert_df, interval=interval, batch_size=batch_size) or 0)
                        if written_rows <= 0 and str(getattr(target_db_provider, "last_error", "")).strip():
                            raise RuntimeError(f"direct_db upsert failed table={table} code={code}: {target_db_provider.last_error}")
                table_report = {
                    "table": table,
                    "source_rows": int(len(source_df)),
                    "existing_rows": int(len(existing_keys)),
                    "missing_rows": int(len(missing_df)),
                    "written_rows": int(written_rows),
                }
                logger.info(
                    f"history sync code={code} table={table} source={table_report['source_rows']} "
                    f"existing={table_report['existing_rows']} missing={table_report['missing_rows']} "
                    f"written={table_report['written_rows']} dry_run={dry_run}"
                )
                if not dry_run and not missing_df.empty:
                    logger.info(
                        f"history sync write result code={code} table={table} "
                        f"requested={len(missing_df)} written={written_rows}"
                    )
                code_report["tables"].append(table_report)
                summary["total_source_rows"] += table_report["source_rows"]
                summary["total_existing_rows"] += table_report["existing_rows"]
                summary["total_missing_rows"] += table_report["missing_rows"]
                summary["total_written_rows"] += table_report["written_rows"]
            summary["code_reports"].append(code_report)
        logger.info(
            f"history sync run finished: total_source={summary['total_source_rows']} "
            f"total_existing={summary['total_existing_rows']} total_missing={summary['total_missing_rows']} "
            f"total_written={summary['total_written_rows']}"
        )
        return summary

    def _check_stop_requested(self, context: str = "") -> None:
        if not self._stop_requested:
            return
        text = str(context or "").strip()
        raise RuntimeError(f"sync stopped by user{(' at ' + text) if text else ''}")

    def _build_target_db_provider(self, write_mode: str, direct_db_source: str):
        if write_mode != "direct_db":
            return None
        if direct_db_source == "mysql":
            return MysqlProvider()
        if direct_db_source == "postgresql":
            return PostgresProvider()
        raise RuntimeError("unsupported direct_db_source")

    def _ensure_target_db_ready(self, write_mode: str, provider: Any, sample_code: str) -> None:
        if write_mode != "direct_db":
            return
        if provider is None:
            raise RuntimeError("direct_db provider not initialized")
        if not hasattr(provider, "check_connectivity"):
            return
        ok, msg = provider.check_connectivity(sample_code)
        if not ok:
            raise RuntimeError(f"direct_db precheck failed: {msg}")

    def _extract_time_keys_from_df(self, df: pd.DataFrame, is_day: bool) -> set[str]:
        if df is None or df.empty:
            return set()
        out: set[str] = set()
        if "dt" in df.columns:
            series = pd.to_datetime(df["dt"], errors="coerce")
            for x in series.dropna().tolist():
                key = self._normalize_time_key(x, is_day=is_day)
                if key:
                    out.add(key)
            return out
        if is_day and "date" in df.columns:
            for x in df["date"].tolist():
                key = self._normalize_time_key(x, is_day=True)
                if key:
                    out.add(key)
            return out
        if (not is_day) and "trade_time" in df.columns:
            for x in df["trade_time"].tolist():
                key = self._normalize_time_key(x, is_day=False)
                if key:
                    out.add(key)
        return out

    def _fetch_existing_keys_from_db(
        self,
        provider: Any,
        table: str,
        code: str,
        start_time: datetime,
        end_time: datetime,
    ) -> set[str]:
        if provider is None:
            return set()
        interval = TABLE_INTERVAL_MAP.get(table, "1min")
        try:
            if hasattr(provider, "fetch_kline_data_strict"):
                df = provider.fetch_kline_data_strict(code, start_time, end_time, interval=interval)
            else:
                df = provider.fetch_kline_data(code, start_time, end_time, interval=interval)
        except Exception as e:
            raise RuntimeError(f"query direct_db existing rows failed table={table} code={code}: {e}")
        provider_err = str(getattr(provider, "last_error", "") or "").strip()
        if provider_err:
            raise RuntimeError(f"query direct_db existing rows failed table={table} code={code}: {provider_err}")
        return self._extract_time_keys_from_df(df, is_day=(table == "dat_days"))

    def _build_direct_db_upsert_df(self, table: str, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame()
        out = df.copy()
        if table == "dat_days":
            if "trade_time" not in out.columns and "date" in out.columns:
                out["trade_time"] = pd.to_datetime(out["date"], errors="coerce")
        return out

    def _build_write_preview(self, table: str, df: pd.DataFrame) -> dict[str, Any]:
        if df is None or df.empty:
            return {"rows": 0}
        key_col = "date" if table == "dat_days" else "trade_time"
        if key_col not in df.columns:
            return {"rows": int(len(df))}
        work = df.copy()
        if table == "dat_days":
            keys = pd.to_datetime(work[key_col], errors="coerce")
            keys = keys.dropna().dt.strftime("%Y-%m-%d")
        else:
            keys = pd.to_datetime(work[key_col], errors="coerce")
            keys = keys.dropna().dt.strftime("%Y-%m-%d %H:%M:%S")
        if keys.empty:
            return {"rows": int(len(df))}
        return {
            "rows": int(len(df)),
            "from": str(keys.iloc[0]),
            "to": str(keys.iloc[-1]),
        }

    def _parse_datetime(self, value: Any) -> Optional[datetime]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value
        text = str(value).strip()
        if not text:
            return None
        text = text.replace("Z", "")
        try:
            return datetime.fromisoformat(text)
        except Exception:
            pass
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                return datetime.strptime(text, fmt)
            except Exception:
                continue
        raise RuntimeError(f"invalid datetime: {value}")

    def _as_bool(self, value: Any, default: bool) -> bool:
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in {"1", "true", "yes", "y", "on"}:
            return True
        if text in {"0", "false", "no", "n", "off"}:
            return False
        return bool(default)

    def _resolve_time_range(
        self,
        payload: dict[str, Any],
        cfg: ConfigLoader,
        lookback_days: int,
        time_mode: str,
        intraday_mode: bool,
    ) -> tuple[datetime, datetime]:
        start_time = self._parse_datetime(payload.get("start_time"))
        end_time = self._parse_datetime(payload.get("end_time"))
        if start_time is not None or end_time is not None:
            end_time = end_time or datetime.now()
            start_time = start_time or (end_time - timedelta(days=lookback_days))
            return start_time, end_time
        if time_mode == "custom":
            custom_start = payload.get("custom_start_time", cfg.get("history_sync.custom_start_time", None))
            custom_end = payload.get("custom_end_time", cfg.get("history_sync.custom_end_time", None))
            start_time = self._parse_datetime(custom_start)
            end_time = self._parse_datetime(custom_end)
            if start_time is None or end_time is None:
                raise RuntimeError("history_sync custom mode requires custom_start_time and custom_end_time")
            return start_time, end_time
        if intraday_mode:
            return self._default_intraday_window()
        end_time = datetime.now()
        start_time = end_time - timedelta(days=lookback_days)
        return start_time, end_time

    def _default_intraday_window(self) -> tuple[datetime, datetime]:
        now = datetime.now()
        start_today = now.replace(hour=9, minute=30, second=0, microsecond=0)
        end_today = now.replace(hour=15, minute=0, second=0, microsecond=0)
        if now < start_today:
            prev = now - timedelta(days=1)
            start_prev = prev.replace(hour=9, minute=30, second=0, microsecond=0)
            end_prev = prev.replace(hour=15, minute=0, second=0, microsecond=0)
            return start_prev, end_prev
        if now >= end_today:
            return start_today, end_today
        return start_today, now

    def _normalize_code(self, code: str) -> str:
        c = str(code or "").strip().upper()
        if not c:
            return c
        if c.isdigit() and len(c) < 6:
            c = c.zfill(6)
        if c.startswith("SH") and len(c) == 8 and c[2:].isdigit():
            return f"{c[2:]}.SH"
        if c.startswith("SZ") and len(c) == 8 and c[2:].isdigit():
            return f"{c[2:]}.SZ"
        if "." in c:
            return c
        if len(c) == 6 and c.isdigit():
            return f"{c}.SH" if c.startswith("6") else f"{c}.SZ"
        return c

    def _normalize_time_key(self, value: Any, is_day: bool) -> Optional[str]:
        if value is None:
            return None
        if isinstance(value, datetime):
            return value.strftime("%Y-%m-%d" if is_day else "%Y-%m-%d %H:%M:%S")
        text = str(value).strip()
        if not text:
            return None
        if is_day:
            try:
                return datetime.fromisoformat(text.replace("Z", "").replace("T", " ")).strftime("%Y-%m-%d")
            except Exception:
                return text[:10]
        try:
            return datetime.fromisoformat(text.replace("Z", "").replace("T", " ")).strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            if len(text) >= 19:
                return text[:19].replace("T", " ")
            return text.replace("T", " ")

    def _to_float(self, value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            if pd.isna(value):
                return None
        except Exception:
            pass
        try:
            out = float(value)
        except Exception:
            return None
        if pd.isna(out):
            return None
        if out == float("inf") or out == float("-inf"):
            return None
        return out

    def _sanitize_rows_for_post(self, table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if not rows:
            return []
        is_day = table == "dat_days"
        time_key = "date" if is_day else "trade_time"
        required = ["code", time_key, "open", "high", "low", "close", "vol", "amount"]
        sanitized: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            code = str(row.get("code", "")).strip().upper()
            if not code:
                continue
            cleaned: dict[str, Any] = {"code": code}
            normalized_time = self._normalize_time_key(row.get(time_key), is_day=is_day)
            if not normalized_time:
                continue
            cleaned[time_key] = normalized_time
            for col in ("open", "high", "low", "close", "vol", "amount"):
                if col in row:
                    cleaned[col] = self._to_float(row.get(col))
            cleaned["vol"] = 0.0 if cleaned.get("vol") is None else cleaned.get("vol")
            cleaned["amount"] = 0.0 if cleaned.get("amount") is None else cleaned.get("amount")
            if any(cleaned.get(k) is None for k in required):
                continue
            sanitized.append(cleaned)
        return sanitized

    def _resolve_api_table_candidates(self, table: str) -> list[str]:
        if table == "dat_days":
            return ["dat_day"]
        return [table]

    def _build_daily_rows_for_api_table(self, api_table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        if api_table == "dat_days":
            return rows
        out: list[dict[str, Any]] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            date_text = self._normalize_time_key(row.get("date"), is_day=True)
            if not date_text:
                continue
            trade_time = self._normalize_time_key(f"{date_text} 00:00:00", is_day=False)
            if not trade_time:
                continue
            mapped = {
                "code": row.get("code"),
                "trade_time": trade_time,
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
                "vol": row.get("vol"),
                "amount": row.get("amount"),
            }
            out.append(mapped)
        return out

    def _resolve_codes(self, payload_codes: Any, max_codes: int) -> list[str]:
        out: list[str] = []
        if isinstance(payload_codes, list):
            out.extend([self._normalize_code(x) for x in payload_codes if str(x).strip()])
        if not out:
            file_path = os.path.join("data", "stock_list.csv")
            if os.path.exists(file_path):
                try:
                    df = pd.read_csv(file_path, dtype=str, keep_default_na=False)
                    if "code" in df.columns:
                        out.extend([self._normalize_code(x) for x in df["code"].tolist()])
                    elif len(df.columns) > 0:
                        out.extend([self._normalize_code(x) for x in df.iloc[:, 0].tolist()])
                except Exception:
                    pass
        if not out:
            cfg = ConfigLoader.reload()
            targets = cfg.get("targets", [])
            if isinstance(targets, list):
                out.extend([self._normalize_code(x) for x in targets if str(x).strip()])
        dedup = []
        seen = set()
        for c in out:
            if not c or c in seen:
                continue
            seen.add(c)
            dedup.append(c)
            if len(dedup) >= max_codes:
                break
        return dedup

    def _build_source_frames(
        self,
        provider: TushareProvider,
        code: str,
        start_time: datetime,
        end_time: datetime,
        tables: list[str],
        session_only: bool = True,
    ) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        minute_tables = [t for t in tables if t != "dat_days"]
        source_by_interval: dict[str, pd.DataFrame] = {}
        self._check_stop_requested(context=f"build source start code {code}")
        if minute_tables:
            base_df = provider.fetch_minute_data(code, start_time, end_time)
            self._check_stop_requested(context=f"after minute fetch code {code}")
            if base_df is not None and not base_df.empty:
                df = base_df.copy()
                if "dt" not in df.columns and "trade_time" in df.columns:
                    df = df.rename(columns={"trade_time": "dt"})
                required = ["dt", "open", "high", "low", "close", "vol", "amount"]
                if not any(col not in df.columns for col in required):
                    df["dt"] = pd.to_datetime(df["dt"], errors="coerce")
                    df = df.dropna(subset=["dt"]).sort_values("dt").drop_duplicates(subset=["dt"])
                    df["open"] = pd.to_numeric(df["open"], errors="coerce")
                    df["high"] = pd.to_numeric(df["high"], errors="coerce")
                    df["low"] = pd.to_numeric(df["low"], errors="coerce")
                    df["close"] = pd.to_numeric(df["close"], errors="coerce")
                    df["vol"] = pd.to_numeric(df["vol"], errors="coerce")
                    df["amount"] = pd.to_numeric(df["amount"], errors="coerce")
                    df = df.dropna(subset=["open", "high", "low", "close"])
                    if session_only:
                        df = self._filter_session_minutes(df)
                    df["code"] = code
                    if not df.empty:
                        source_by_interval["1min"] = df
                        needed_intervals = {TABLE_INTERVAL_MAP[t] for t in minute_tables}
                        for interval in needed_intervals:
                            if interval == "1min":
                                continue
                            source_by_interval[interval] = Indicators.resample(df.copy(), interval)
                            source_by_interval[interval]["code"] = code
        for table in tables:
            self._check_stop_requested(context=f"build source table {table} code {code}")
            if table == "dat_days":
                day_df = provider.fetch_daily_data(code, start_time, end_time)
                self._check_stop_requested(context=f"after daily fetch code {code}")
                if day_df is None or day_df.empty:
                    frames[table] = pd.DataFrame()
                    continue
                day_df["dt"] = pd.to_datetime(day_df["dt"], errors="coerce")
                day_df = day_df.dropna(subset=["dt"])
                day_df = day_df[(day_df["dt"] >= start_time) & (day_df["dt"] <= end_time)]
                if day_df.empty:
                    frames[table] = pd.DataFrame()
                    continue
                day_df = day_df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
                day_df["date"] = day_df["dt"].dt.strftime("%Y-%m-%d")
                day_df["pre_close"] = day_df["close"].shift(1).fillna(day_df["close"])
                day_df["change"] = (day_df["close"] - day_df["pre_close"]).fillna(0.0)
                day_df["pct_chg"] = (day_df["change"] / day_df["pre_close"] * 100.0).replace([float("inf"), float("-inf")], 0.0).fillna(0.0)
                use_cols = [
                    "code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "vol",
                    "amount",
                    "pre_close",
                    "change",
                    "pct_chg",
                ]
                frames[table] = day_df[use_cols].copy().reset_index(drop=True)
                continue
            interval = TABLE_INTERVAL_MAP[table]
            interval_df = source_by_interval.get(interval)
            if interval_df is None or interval_df.empty:
                frames[table] = pd.DataFrame()
                continue
            table_df = interval_df.copy()
            if "dt" not in table_df.columns:
                frames[table] = pd.DataFrame()
                continue
            table_df["dt"] = pd.to_datetime(table_df["dt"], errors="coerce")
            table_df = table_df.dropna(subset=["dt"])
            table_df = table_df[(table_df["dt"] >= start_time) & (table_df["dt"] <= end_time)]
            if table_df.empty:
                frames[table] = pd.DataFrame()
                continue
            table_df = table_df.sort_values("dt").drop_duplicates(subset=["dt"]).reset_index(drop=True)
            table_df["date"] = table_df["dt"].dt.strftime("%Y-%m-%d")
            table_df["pre_close"] = table_df["close"].shift(1)
            table_df["change"] = table_df["close"] - table_df["pre_close"]
            table_df["pct_chg"] = table_df["change"] / table_df["pre_close"] * 100.0
            table_df["pre_close"] = table_df["pre_close"].fillna(table_df["close"])
            table_df["change"] = table_df["change"].fillna(0.0)
            table_df["pct_chg"] = table_df["pct_chg"].replace([pd.NA, pd.NaT], 0.0).fillna(0.0)
            for col in ("open", "high", "low", "close", "vol", "amount", "pre_close", "change", "pct_chg"):
                table_df[col] = pd.to_numeric(table_df[col], errors="coerce")
            table_df = table_df.dropna(subset=["open", "high", "low", "close"])
            table_df["vol"] = table_df["vol"].fillna(0.0)
            table_df["amount"] = table_df["amount"].fillna(0.0)
            table_df["pre_close"] = table_df["pre_close"].fillna(table_df["close"])
            table_df["change"] = table_df["change"].fillna(0.0)
            table_df["pct_chg"] = table_df["pct_chg"].replace([float("inf"), float("-inf")], 0.0).fillna(0.0)
            if table == "dat_days":
                use_cols = [
                    "code",
                    "date",
                    "open",
                    "high",
                    "low",
                    "close",
                    "vol",
                    "amount",
                    "pre_close",
                    "change",
                    "pct_chg",
                ]
                out_df = table_df[use_cols].copy()
            else:
                table_df["trade_time"] = table_df["dt"].dt.strftime("%Y-%m-%d %H:%M:%S")
                use_cols = [
                    "code",
                    "trade_time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "vol",
                    "amount",
                    "date",
                    "pre_close",
                    "change",
                    "pct_chg",
                ]
                out_df = table_df[use_cols].copy()
            frames[table] = out_df.reset_index(drop=True)
        return frames

    def _filter_session_minutes(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty or "dt" not in df.columns:
            return pd.DataFrame() if df is None else df
        work = df.copy()
        work["dt"] = pd.to_datetime(work["dt"], errors="coerce")
        work = work.dropna(subset=["dt"])
        minutes = work["dt"].dt.hour * 60 + work["dt"].dt.minute
        mask = (minutes >= 9 * 60 + 30) & (minutes <= 15 * 60)
        work = work.loc[mask].copy()
        return work.reset_index(drop=True)

    def _fetch_existing_keys(
        self,
        session: requests.Session,
        base_url: str,
        headers: dict[str, str],
        table: str,
        code: str,
        start_time: datetime,
        end_time: datetime,
    ) -> set[str]:
        offset = 0
        limit = 10000
        result: set[str] = set()
        last_error = ""
        api_tables = self._resolve_api_table_candidates(table)
        for api_table in api_tables:
            path = f"{base_url}/tables/{api_table}/rows"
            if table == "dat_days":
                if api_table == "dat_days":
                    query_plans = [
                        (
                            "date",
                            "date",
                            [
                                f"code:eq:{code}",
                                f"date:gte:{start_time.strftime('%Y-%m-%d')}",
                                f"date:lte:{end_time.strftime('%Y-%m-%d')}",
                            ],
                        ),
                        (
                            "trade_time",
                            "trade_time",
                            [
                                f"code:eq:{code}",
                                f"trade_time:gte:{start_time.strftime('%Y-%m-%d 00:00:00')}",
                                f"trade_time:lte:{end_time.strftime('%Y-%m-%d 23:59:59')}",
                            ],
                        ),
                    ]
                else:
                    query_plans = [
                        (
                            "trade_time",
                            "trade_time",
                            [
                                f"code:eq:{code}",
                                f"trade_time:gte:{start_time.strftime('%Y-%m-%d 00:00:00')}",
                                f"trade_time:lte:{end_time.strftime('%Y-%m-%d 23:59:59')}",
                            ],
                        ),
                    ]
            else:
                query_plans = [
                    (
                        "trade_time",
                        "trade_time",
                        [
                            f"code:eq:{code}",
                            f"trade_time:gte:{start_time.strftime('%Y-%m-%d %H:%M:%S')}",
                            f"trade_time:lte:{end_time.strftime('%Y-%m-%d %H:%M:%S')}",
                        ],
                    ),
                ]
            for key_col, order_by, filters in query_plans:
                result.clear()
                offset = 0
                ok = True
                while True:
                    self._check_stop_requested(context=f"query existing table {table} code {code}")
                    params = {
                        "limit": limit,
                        "offset": offset,
                        "order_by": order_by,
                        "order_dir": "asc",
                        "filter": filters,
                    }
                    resp = session.get(path, headers=headers, params=params, timeout=45)
                    if resp.status_code != 200:
                        ok = False
                        last_error = f"table={api_table} status={resp.status_code} detail={resp.text[:200]}"
                        break
                    payload = resp.json()
                    rows = payload.get("rows") if isinstance(payload, dict) else payload
                    if not isinstance(rows, list) or len(rows) == 0:
                        break
                    for row in rows:
                        if isinstance(row, dict) and row.get(key_col) is not None:
                            normalized_key = self._normalize_time_key(row.get(key_col), is_day=(table == "dat_days"))
                            if normalized_key:
                                result.add(normalized_key)
                    if len(rows) < limit:
                        break
                    offset += limit
                if ok:
                    return result
        raise RuntimeError(f"query existing rows failed table={table} code={code} {last_error}")

    def _push_rows(
        self,
        session: requests.Session,
        base_url: str,
        headers: dict[str, str],
        table: str,
        rows: list[dict[str, Any]],
        batch_size: int,
        on_duplicate: str,
    ) -> int:
        if not rows:
            return 0
        written = 0
        api_tables = self._resolve_api_table_candidates(table)
        for i in range(0, len(rows), batch_size):
            self._check_stop_requested(context=f"push rows table {table}")
            batch = self._sanitize_rows_for_post(table=table, rows=rows[i:i + batch_size])
            if not batch:
                continue
            inserted = False
            last_error = ""
            for api_table in api_tables:
                path = f"{base_url}/tables/{api_table}/rows"
                post_rows = self._build_daily_rows_for_api_table(api_table, batch) if table == "dat_days" else batch
                if not post_rows:
                    continue
                payload = {"on_duplicate": on_duplicate, "rows": post_rows}
                resp = session.post(path, headers=headers, json=payload, timeout=90)
                if resp.status_code != 200:
                    last_error = f"table={api_table} status={resp.status_code} detail={resp.text[:200]}"
                    continue
                data = resp.json()
                rowcount = data.get("rowcount") if isinstance(data, dict) else None
                if isinstance(rowcount, int):
                    written += rowcount
                else:
                    written += len(post_rows)
                inserted = True
                break
            if not inserted:
                raise RuntimeError(f"insert rows failed table={table} {last_error}")
        return written
