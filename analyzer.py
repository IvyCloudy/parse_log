"""日志分析服务层。

封装一个常驻的 Spell 解析器（对应论文「在线流式解析器」），并提供：
    - 按 (服务单元, 时间窗) 流式拉取日志并增量解析
    - 模板查询 / Top-N 错误 / 维度(group-by)统计
    - 模板库持久化(save/load)

数据源通过 data_sources.LogDataSource 注入，与具体接口解耦。
"""

from __future__ import annotations

import threading
import time
from typing import Dict, List, Optional

from spell_parser import Spell, LCSObject
from data_sources import LogDataSource, log_date_to_ms


# 错误类型分类规则：按关键字优先级匹配模板内容。
#   网络异常：连接/超时/通信类
#   平台问题：中间件/框架/连接池/事务组件本身
#   业务异常：数据约束/SQL 逻辑/映射类
_ERROR_RULES = [
    ("网络异常", [
        "timeout", "timed out", "time out", "communications link failure",
        "broken pipe", "connection refused", "could not connect",
        "connection is not available", "connection reset", "no route",
        "redisconnection", "mongosocket", "connectexception", "sockettimeoutexception",
    ]),
    ("平台问题", [
        "hikaripool", "lettuce", "transaction timed out", "transactiontimedoutexception",
        "lazyinitializationexception", "sqlexceptionhelper", "connection pool",
        "max pool size", "pool is exhausted",
    ]),
    ("业务异常", [
        "deadlock", "duplicate entry", "lock wait timeout", "foreign key",
        "data too long", "sql syntax", "reflectionexception",
        "integrityconstraintviolation", "constraint fails", "could not set property",
        "bad sql grammar",
    ]),
]


def _load_error_rules():
    """错误分类规则，支持通过环境变量 LOG_ERROR_RULES 覆盖（JSON 数组）。"""
    import os
    import json as _json
    env = os.environ.get("LOG_ERROR_RULES")
    if env:
        try:
            return _json.loads(env)
        except Exception:
            pass
    return _ERROR_RULES


_ERROR_RULES_RUNTIME = _load_error_rules()


def classify_error_type(template: str) -> str:
    """根据模板内容判定错误大类：网络异常 / 平台问题 / 业务异常 / 未知。

    规则可通过环境变量 LOG_ERROR_RULES（JSON: [[label, [keys...]], ...]）覆盖，
    便于不同业务定制分类。无规则命中时返回「未知」，避免把所有未分类日志误归为业务异常。
    """
    low = template.lower()
    for label, keys in _ERROR_RULES_RUNTIME:
        if any(k in low for k in keys):
            return label
    return "未知"  # 兜底：显式标记未分类，便于后续补充规则


def _rebuild_spell(data: dict, tau: float) -> "Spell":
    """从 to_dict() 产物重建一个 Spell 实例（load 仅接受路径，这里提供 dict 重载）。"""
    sp = Spell(tau=tau)
    sp.total_processed = data.get("total_processed", 0)
    sp.stats = data.get("stats", sp.stats)
    for t in data.get("types", []):
        obj = LCSObject(t["seq"], -1, ts=t.get("last_seen"))
        obj.count = t["count"]
        obj.line_ids.discard(-1)
        obj.line_ids.update(t.get("line_ids") or [])
        obj.params_sample = t.get("params_sample") or []
        sp.lcs_map.append(obj)
        sp.tree.insert(obj.seq, sp._next_id)
        sp._next_id += 1
    return sp


class ConcurrentFeedError(RuntimeError):
    """检测到并发/并行喂入时抛出。

    Spell 解析器是「单实例在线流式」语义：模板库(LCSMap)常驻内存、按时间递增
    顺序 one-pass 喂入。若多个线程/请求同时调用 analyze 并行喂入，会破坏模板
    合并的正确性（同一份状态被交错写入）。本异常用于主动拦截这种误用。
    """


class OutOfOrderFeedError(RuntimeError):
    """检测到乱序喂入时抛出。

    流式解析要求后一次 analyze 的起始时间不早于上一次已处理的截止时间（或重叠
    可控）。若出现时间倒退，说明分页/多线程把历史日志又喂了进来，会污染模板。
    """


class LogAnalyzer:
    # 内部时间窗切片大小（仅用于分批查询，不影响统计结果语义）
    _WINDOW_MS = 3_600_000

    def __init__(self, source: LogDataSource, tau: float = 0.5):
        self.source = source
        self.spell = Spell(tau=tau)
        # 维度分桶收归到 LogAnalyzer（不再挂在 Spell 实例上污染解析器内部状态）。
        # key = 维度值（如 appName=order-app），value = 该维度独立的 Spell 解析器。
        self._by_dim: Dict[str, Spell] = {}
        # 保护整个 analyze 流式喂入过程的锁（杜绝并行喂入）
        self._feed_lock = threading.RLock()
        # 当前持有喂入的线程标识与进入时间戳（用于并行检测与诊断）
        self._feed_owner: Optional[int] = None
        self._feed_owner_enter: float = 0.0
        # 上一次成功喂入覆盖到的时间上界（用于乱序检测），None 表示尚未喂入
        self._last_fed_end_ms: Optional[int] = None

    # -- 流式分析驱动 ------------------------------------------------------

    def analyze(self, services: Optional[List[str]] = None,
                start_ms: int = 1787278400000, end_ms: int = 1787282000000,
                batch_size: int = 50, by_dim: Optional[str] = None,
                allow_out_of_order: bool = False) -> dict:
        """按 (服务单元, 时间窗) 分批查询并增量解析，返回本次分析概览。

        保护机制（防误用）：
          1. 并行喂入保护：同一时刻只允许一个线程执行 analyze。若检测到其它线程
             正在喂入，直接抛出 ConcurrentFeedError，绝不允许多线程交错写入模板库。
          2. 乱序喂入保护：流式解析要求时间递增。若本次 start 早于上一次已覆盖到
             的时间上界（倒退），抛出 OutOfOrderFeedError。可通过 allow_out_of_order
             关闭此检查（仅当你确实要重新训练模板库时）。
        """
        # —— 并行喂入检测/保护 ——
        # 先尝试获取锁（非阻塞），若拿不到说明有别的线程正在 analyze 中。
        if not self._feed_lock.acquire(blocking=False):
            owner = self._feed_owner
            raise ConcurrentFeedError(
                "检测到并行/并发喂入：Spell 解析器为单实例在线流式语义，"
                "同一时刻只允许一个 analyze 在运行。请改为串行调用，或将本服务"
                "部署为单实例、用队列串行消费日志。当前占用线程 tid="
                f"{owner}。"
            )
        try:
            # 二次确认持有者（RLock 同一线程可重入；跨线程不会同时进入此块）
            self._feed_owner = threading.get_ident()
            self._feed_owner_enter = time.time()

            # —— 乱序喂入检测/保护 ——
            if (not allow_out_of_order and self._last_fed_end_ms is not None
                    and start_ms < self._last_fed_end_ms):
                raise OutOfOrderFeedError(
                    f"检测到乱序喂入：本次 start_ms={start_ms} 早于已处理到的"
                    f"时间上界 last_end_ms={self._last_fed_end_ms}。流式解析要求"
                    "按时间递增顺序喂入；如需重新训练请先调用 reset() 或显式传入 "
                    "allow_out_of_order=True。"
                )

            services = services or self.source.list_services()
            before = self.spell.total_processed

            # 分析前已存在的模板集合（用于判定「持续存在 / 新增」）
            pre_existing = {o.template() for o in self.spell.lcs_map}

            # 趋势分桶（默认按时间窗均分 10 桶）
            bucket_ms = max(1, (end_ms - start_ms) // 10)

            # 本次分析窗口内的「模式记录」：模板 -> 累计信息
            patterns: Dict[str, dict] = {}

            window_total = 0
            for service in services:
                w_start = start_ms
                while w_start <= end_ms:
                    w_end = min(w_start + self._WINDOW_MS, end_ms)
                    cur = w_start
                    while True:
                        page = self.source.query_page(service, cur, w_end, batch_size)
                        if not page.items:
                            break
                        for log in page.items:
                            msg = log.get("message", "")
                            lm = log_date_to_ms(log.get("logDate", ""))
                            # 用「日志本身的时间」作为 ts（而非处理时间 time.time()），
                            # 否则 last_seen / 趋势 / 乱序检测都会基于错误的处理时刻。
                            obj = self.spell.add(msg, ts=lm if lm else None)
                            tmpl = obj.template()
                            # 维度分桶：把同一条日志也喂给对应维度的独立 Spell，
                            # 提升各维度模板隔离度（主 spell 仍保留全局概览）。
                            if by_dim is not None:
                                dim_val = log.get(by_dim)
                                if dim_val:
                                    sub = self._by_dim.get(dim_val)
                                    if sub is None:
                                        sub = Spell(tau=self.spell.tau)
                                        self._by_dim[dim_val] = sub
                                    sub.add(msg, ts=lm if lm else None)

                            # 累加本模式在时间窗内的统计
                            lm = log_date_to_ms(log.get("logDate", ""))
                            bucket_idx = min(9, max(0, (lm - start_ms) // bucket_ms))
                            rec = patterns.get(tmpl)
                            if rec is None:
                                rec = {
                                    "template": tmpl,
                                    "count": 0,
                                    "first_ms": lm,
                                    "last_ms": lm,
                                    "sample": msg,
                                    "buckets": [0] * 10,
                                    "change_type": "新增" if tmpl not in pre_existing else "持续存在",
                                }
                                patterns[tmpl] = rec
                            rec["count"] += 1
                            if lm and (rec["first_ms"] == 0 or lm < rec["first_ms"]):
                                rec["first_ms"] = lm
                            if lm and (rec["last_ms"] == 0 or lm > rec["last_ms"]):
                                rec["last_ms"] = lm
                            rec["buckets"][bucket_idx] += 1
                            window_total += 1
                        if page.next_start is None:
                            break
                        cur = page.next_start
                    w_start = w_end + 1

            # 本次声明覆盖到的时间上界，用于乱序检测（按请求区间记录，而非分页游标）
            self._last_fed_end_ms = max(
                self._last_fed_end_ms if self._last_fed_end_ms is not None else end_ms,
                end_ms,
            )

            processed = self.spell.total_processed - before

            # 组装模式列表（按数量降序）
            pattern_list = []
            for rec in sorted(patterns.values(), key=lambda r: r["count"], reverse=True):
                cnt = rec["count"]
                pattern_list.append({
                    "template": rec["template"],
                    "count": cnt,
                    "ratio": round(cnt / window_total * 100, 2) if window_total else 0.0,
                    "first_seen_ms": rec["first_ms"],
                    "last_seen_ms": rec["last_ms"],
                    "trend": [
                        {"bucket_start_ms": start_ms + i * bucket_ms, "count": c}
                        for i, c in enumerate(rec["buckets"])
                    ],
                    "change_type": rec["change_type"],
                    "error_type": classify_error_type(rec["template"]),
                    "sample": rec["sample"],
                })

            return {
                "services": services,
                "newly_processed": processed,
                "total_processed": self.spell.total_processed,
                "window_total": window_total,
                "message_types": len(pattern_list),
                "patterns": pattern_list,
            }
        finally:
            self._feed_owner = None
            self._feed_lock.release()

    # -- 查询 --------------------------------------------------------------

    def templates(self, service: Optional[str] = None,
                  limit: int = 20) -> List[dict]:
        """返回模板列表 (模板, 计数)。可指定 service/维度值查询独立分桶。"""
        spell = self.spell
        if service and service in self._by_dim:
            spell = self._by_dim[service]
        return [
            {"template": tmpl, "count": cnt}
            for tmpl, cnt in spell.templates()[:limit]
        ]

    def summary(self) -> dict:
        s = self.spell
        return {
            "total_processed": s.total_processed,
            "message_types": len(s.lcs_map),
            "prefilter_stats": s.stats,
            "top_templates": [
                {"template": tmpl, "count": cnt}
                for tmpl, cnt in s.templates()[:10]
            ],
        }

    def services(self) -> List[str]:
        return self.source.list_services()

    def reset(self) -> None:
        """清空解析器，重新冷启动。

        同时重置流式保护状态：已覆盖的时间上界、喂入持有者，避免残留导致后续误报乱序。
        """
        self.spell = Spell(tau=self.spell.tau)
        self._by_dim = {}
        self._last_fed_end_ms = None
        self._feed_owner = None

    # -- 持久化 ------------------------------------------------------------

    def save(self, path: str) -> None:
        self.spell.save(path)
        # 维度分桶单独持久化，文件名加 .dim 后缀
        import json as _json
        dim_path = path + ".dim"
        with open(dim_path, "w", encoding="utf-8") as f:
            _json.dump(
                {k: v.to_dict() for k, v in self._by_dim.items()},
                f, ensure_ascii=False, indent=2,
            )

    def load(self, path: str) -> None:
        self.spell = Spell.load(path)
        import json as _json
        dim_path = path + ".dim"
        self._by_dim = {}
        try:
            with open(dim_path, "r", encoding="utf-8") as f:
                raw = _json.load(f)
            for k, d in raw.items():
                self._by_dim[k] = _rebuild_spell(d, self.spell.tau)
        except FileNotFoundError:
            pass
