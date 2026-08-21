"""日志数据源抽象层（正式数据源：HTTP / 本地文件）。

屏蔽不同日志查询接口的差异，统一为两个方法：
    list_services()                      -> 该数据源包含哪些服务单元(appName)
    query_page(service, start_ms, end_ms, limit) -> LogPage(该服务在 [start,end] 内的日志)

返回结构统一为 LogPage，内部每条日志是 log-format.json 中的 logData 对象。

注意：Mock 数据源（用于本地开发/演示）不在此文件，单独放在 mock_source.py，
避免与正式生产代码混在一起。
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional


class LogPage:
    """接口返回的一页日志。

    items: List[dict]，每条为 logDatas 中的一条完整日志对象（含 message 等字段）
    next_start: 下一页查询的起始时间戳(ms)；为 None 表示没有更多数据
    """

    def __init__(self, items: List[dict], next_start: Optional[int]):
        self.items = items
        self.next_start = next_start


def log_date_to_ms(log_date: str) -> int:
    """把日志 logDate (如 '2026-06-15T10:00:00.0000000+0800') 转成毫秒时间戳。

    保留毫秒精度与时区（原实现 split('+')[0].split('.')[0] 直接丢时区与毫秒，
    导致分页游标精度不足、trend/乱序分析基于错误时间）。兼容 ±HHMM / ±HH:MM / Z /
    无时区 / 任意小数位等情况；解析失败返回 0。
    """
    import datetime
    if not log_date:
        return 0
    s = log_date.strip().replace("Z", "+0000")
    # 截断小数秒到微秒（fromisoformat 最多 6 位），保留精度
    s = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6], s)
    if "+" in s:
        head, tz = s.split("+", 1)
        s = head + "+" + (tz[:2] + ":" + tz[2:4] if len(tz) >= 4 else tz)
    elif "-" in s[10:]:
        date_part, rest = s[:10], s[10:]
        head, tz = rest.split("-", 1)
        s = date_part + head + "-" + (tz[:2] + ":" + tz[2:4] if len(tz) >= 4 else tz)
    try:
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return int(dt.timestamp() * 1000)
    except Exception:
        for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S"):
            try:
                dt = datetime.datetime.strptime(s[:19], fmt)
                return int(dt.replace(tzinfo=datetime.timezone.utc).timestamp() * 1000)
            except Exception:
                continue
        return 0


class LogDataSource:
    """数据源基类，定义统一接口。"""

    def list_services(self) -> List[str]:
        raise NotImplementedError

    def query_page(self, service: str, start_ms: int, end_ms: int,
                   limit: int = 50) -> LogPage:
        raise NotImplementedError


class HttpSource(LogDataSource):
    """真实 HTTP 日志接口数据源（薄封装）。

    实际的「按服务单元+时间窗调用外部日志接口、解析响应、分页」逻辑全部在
    独立的 log_client.py 中（应对复杂接口：POST / 签名鉴权 / 游标分页 / 字段映射等）。
    本类仅做适配：把统一接口 query_page(service, start, end, limit) 转给 LogClient。
    """

    def __init__(self, base_url: str, token: str = "", batch_size: int = 500,
                 services: Optional[List[str]] = None, client_config: Optional[object] = None):
        from log_client import LogClient, LogClientConfig
        self.base_url = base_url
        self.token = token
        self.batch_size = batch_size
        self._services = services  # 服务单元 ID (serviceUinitId) 列表
        # 复杂接口契约可用 client_config 定制；默认按 log-format.json
        if client_config is None:
            client_config = LogClientConfig(url=base_url, token=token)
        self.client = LogClient(client_config)

    def list_services(self) -> List[str]:
        if self._services is not None:
            return list(self._services)
        raise NotImplementedError("请实现服务发现 endpoint，或用 services=[] 指定")

    def query_page(self, service: str, start_ms: int, end_ms: int,
                   limit: int = 500, cursor: Optional[str] = None) -> LogPage:
        # cursor 透传给 LogClient（time 滚动时由 analyzer 用整数 next_start 传入）
        return self.client.query(service, start_ms, end_ms, limit, cursor)


class FileSource(LogDataSource):
    """本地 JSONL 文件数据源（每行一条 logData）。

    分页/发现均以 serviceUinitId（服务单元 ID）为维度；appName 仅作应用名称。
    """

    def __init__(self, path: str, batch_size: int = 50):
        self.path = path
        self.batch_size = batch_size
        self.rows: List[dict] = []
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        self.rows.append(json.loads(line))
                    except json.JSONDecodeError:
                        pass
        # 按 (serviceUinitId, 时间戳, 行号) 建索引并排序，使分页可顺序推进。
        # 行号作为同毫秒多条的二级键，避免游标卡死/丢数据（原实现 next_start=ts+1
        # 在多条同毫秒日志时会重复或跳过）。
        self._by_service: Dict[str, List[tuple]] = {}
        for idx, r in enumerate(self.rows):
            svc = r.get("serviceUinitId", "unknown")
            ts = log_date_to_ms(r.get("logDate", ""))
            self._by_service.setdefault(svc, []).append((ts, idx, r))
        for lst in self._by_service.values():
            lst.sort(key=lambda t: (t[0], t[1]))
        # 各服务的消费游标（索引位置），保证跨页不丢不重
        self._pos: Dict[str, int] = {svc: 0 for svc in self._by_service}

    def list_services(self) -> List[str]:
        # 返回服务单元 ID 集合（serviceUinitId），缺失时回退 unknown
        return sorted(self._by_service)

    def query_page(self, service: str, start_ms: int, end_ms: int,
                   limit: int = 50) -> LogPage:
        seq = self._by_service.get(service)
        if not seq:
            return LogPage(items=[], next_start=None)
        # 若 start_ms 比当前游标处的时间更靠前，重置游标到 start_ms 之前（二分定位）
        pos = self._pos.get(service, 0)
        if pos >= len(seq) or seq[pos][0] > start_ms:
            # 二分找第一个 ts >= start_ms 的位置
            lo, hi = 0, len(seq)
            while lo < hi:
                mid = (lo + hi) // 2
                if seq[mid][0] < start_ms:
                    lo = mid + 1
                else:
                    hi = mid
            pos = lo
            self._pos[service] = pos
        # 从游标顺序取 [start_ms, end_ms] 内的日志，最多 limit 条
        chunk = []
        i = pos
        while i < len(seq) and len(chunk) < limit:
            ts, _idx, row = seq[i]
            if ts > end_ms:
                break
            if ts >= start_ms:
                chunk.append(row)
            i += 1
        self._pos[service] = i
        # 还有同服务且时间窗之后的日志才继续
        has_more = i < len(seq) and seq[i][0] <= end_ms
        next_start = (seq[i][0] if has_more else None)
        return LogPage(items=chunk, next_start=next_start)


def build_source(source_type: str, *, api_url: str = "", api_token: str = "",
                 file_path: str = "", services: Optional[str] = None,
                 batch_size: int = 50, mock_total: int = 12000) -> LogDataSource:
    """统一数据源构建入口，供 main.py 与 api.py 复用，避免双份真相（问题 #12）。

    source_type: mock | file | http
    """
    if source_type == "mock":
        from demo.mock_source import MockSource
        return MockSource(batch_size=batch_size, total=mock_total)
    if source_type == "file":
        if not file_path:
            raise RuntimeError("LOG_SOURCE=file 需设置 LOG_FILE（或 config.yaml data_source.file）")
        return FileSource(file_path, batch_size=batch_size)
    # 默认 http：由 log_client.py 负责调用真实日志查询接口
    if not api_url:
        raise RuntimeError("LOG_SOURCE=http 需设置 LOG_API_URL（或 config.yaml data_source.api_url；"
                           "或改用 LOG_SOURCE=mock 演示）")
    svcs = services.split(",") if services else None
    return HttpSource(api_url, api_token, batch_size=batch_size, services=svcs)
