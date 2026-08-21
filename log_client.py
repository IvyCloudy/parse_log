"""真实日志查询接口的 HTTP 客户端（独立文件）。

职责单一：根据「服务单元 + 起止时间」等入参，调用外部日志查询接口，
把响应规整成统一的 logData 列表 + 翻页游标，返回 LogPage。

把接口复杂性（请求构造 / 鉴权 / 响应解析 / 分页策略）全部收敛在本文件，
不污染数据源抽象(data_sources)与解析逻辑(analyzer/spell_parser)。

默认契约遵循 log-format.json：
    GET {url}?service=<serviceUinitId>&startTime=<ms>&endTime=<ms>&limit=<n>
    -> { "code":0, "data": { "logDatas": [ {message, logDate, appName, serviceUinitId, ...} ] } }

真实接口若更复杂（POST body / 签名鉴权 / cursor 分页 / 字段名不同），
通过 LogClientConfig 配置，或继承 LogClient 重写钩子，核心分析流程无需改动。
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from data_sources import LogPage, log_date_to_ms


@dataclass
class LogClientConfig:
    """外部日志查询接口契约配置（应对复杂接口）。

    占位符可在 url / headers / body 中使用：
        {service} {start} {end} {limit} {cursor}
    """
    # 基础信息
    url: str = ""
    method: str = "GET"                 # GET / POST
    timeout: float = 10.0
    max_retries: int = 3
    retry_backoff: float = 0.5

    # 鉴权
    auth_type: str = "bearer"           # bearer / apikey / none
    token: str = ""
    apikey_header: str = "X-API-Key"
    extra_headers: Dict[str, str] = field(default_factory=dict)

    # 请求体（POST 时使用；支持占位符）
    body_template: Optional[Dict[str, Any]] = None

    # 响应解析
    log_datas_path: str = "data.logDatas"   # 取列表的 dotted 路径
    field_message: str = "message"
    field_logdate: str = "logDate"
    field_appname: str = "appName"
    field_unit: str = "serviceUinitId"

    # 分页策略：time(默认, maxTs+1) / cursor(响应里取 next) / offset
    pagination: str = "time"
    cursor_path: str = "data.next"          # pagination=cursor 时取下一页游标
    offset_param: str = "offset"            # pagination=offset 时偏移量字段名


def _get_path(obj: Dict[str, Any], path: str) -> Any:
    cur: Any = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur


class LogClient:
    """调用外部日志查询接口，返回规整后的 LogPage。

    使用方式（在 HttpSource 内被调用）：
        client = LogClient(config)
        page = client.query(service, start_ms, end_ms, limit, cursor=None)
    """

    def __init__(self, config: LogClientConfig):
        self.cfg = config

    # ---- 公开方法 ----------------------------------------------------------
    def query(self, service: str, start_ms: int, end_ms: int,
              limit: int, cursor: Optional[str] = None) -> LogPage:
        items, next_cursor = self._fetch(service, start_ms, end_ms, limit, cursor)
        # 统一规整字段，确保含 appName / serviceUinitId
        norm = [self._normalize(r) for r in items]
        # 计算翻页游标
        next_start = self._compute_next_start(norm, next_cursor, start_ms, end_ms)
        return LogPage(items=norm, next_start=next_start)

    # ---- 内部：请求 --------------------------------------------------------
    def _build_request(self, service, start_ms, end_ms, limit, cursor):
        cfg = self.cfg
        headers = dict(cfg.extra_headers)
        if cfg.auth_type == "bearer" and cfg.token:
            headers["Authorization"] = f"Bearer {cfg.token}"
        elif cfg.auth_type == "apikey" and cfg.token:
            headers[cfg.apikey_header] = cfg.token

        params, body = None, None
        if cfg.method.upper() == "GET":
            params = {
                "service": service,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            }
            if cfg.pagination == "offset" and cursor is not None:
                params[cfg.offset_param] = cursor
            elif cfg.pagination == "cursor" and cursor:
                params["cursor"] = cursor
        else:  # POST
            if cfg.body_template:
                body = json.loads(json.dumps(cfg.body_template)
                                  .replace("{service}", str(service))
                                  .replace("{start}", str(start_ms))
                                  .replace("{end}", str(end_ms))
                                  .replace("{limit}", str(limit))
                                  .replace("{cursor}", str(cursor or "")))
            else:
                body = {
                    "service": service,
                    "startTime": start_ms,
                    "endTime": end_ms,
                    "limit": limit,
                    "cursor": cursor,
                }
        return headers, params, body

    def _fetch(self, service, start_ms, end_ms, limit, cursor):
        cfg = self.cfg
        headers, params, body = self._build_request(service, start_ms, end_ms, limit, cursor)
        last_err = None
        for attempt in range(cfg.max_retries + 1):
            try:
                resp = requests.request(
                    cfg.method, cfg.url, headers=headers,
                    params=params, json=body, timeout=cfg.timeout,
                )
                resp.raise_for_status()
                data = resp.json()
                items = _get_path(data, cfg.log_datas_path) or []
                items = [x for x in items if isinstance(x, dict)]
                next_cursor = _get_path(data, cfg.cursor_path) if cfg.pagination == "cursor" else None
                return items, next_cursor
            except Exception as e:  # noqa: BLE001
                last_err = e
                if attempt < cfg.max_retries:
                    time.sleep(cfg.retry_backoff * (attempt + 1))
        raise RuntimeError(f"日志查询接口调用失败(重试{cfg.max_retries}次): {last_err}")

    # ---- 内部：规整与翻页 --------------------------------------------------
    def _normalize(self, raw: Dict[str, Any]) -> Dict[str, Any]:
        cfg = self.cfg
        out = dict(raw)
        # 确保关键字段存在（用配置映射，缺省保持原名）
        out["message"] = raw.get(cfg.field_message, raw.get("message", ""))
        out["logDate"] = raw.get(cfg.field_logdate, raw.get("logDate", ""))
        out["appName"] = raw.get(cfg.field_appname, raw.get("appName", "unknown"))
        out["serviceUinitId"] = raw.get(cfg.field_unit, raw.get("serviceUinitId", "unknown"))
        return out

    def _compute_next_start(self, items, next_cursor, start_ms, end_ms):
        cfg = self.cfg
        if cfg.pagination == "cursor":
            return next_cursor  # 游标直接透传（字符串）
        if cfg.pagination == "offset":
            return None if len(items) < 1 else "offset+continues"  # 占位，HttpSource 用 offset 推进
        # time 滚动：本批最大 logDate(ms)+1
        if not items:
            return None
        max_ts = max(log_date_to_ms(r.get("logDate", "")) for r in items)
        return max_ts + 1 if max_ts > 0 else None
