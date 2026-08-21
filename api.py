"""FastAPI 应用：把日志分析功能对外提供为 HTTP 服务。

对外核心接口（业务使用）只有 1 个：
    POST /analyze
        入参决定分析哪个服务单元 + 时间窗；服务端据此调用日志查询接口拿到日志，
        再做在线流式解析，并直接返回模板结果（形成闭环）。

其余端点（/health、/services、/templates、/summary、/reset、/save、/load）
为内部/调试辅助接口，便于排查与运维，不作为对外业务契约。

数据源选择（配置文件 config.yaml 或环境变量，环境优先）：
    LOG_SOURCE=mock | file | http
    - mock: demo/mock_source.py（本地生成数据库报错，仅演示，不进正式代码）
    - file: 本地 JSONL
    - http: 由 log_client.py 调用真实日志查询接口（接口复杂性封装在该文件）
"""

from __future__ import annotations

import logging
import threading
from typing import List, Optional

from fastapi import FastAPI, HTTPException, Query

from config import settings
from analyzer import LogAnalyzer, ConcurrentFeedError, OutOfOrderFeedError
from data_sources import build_source
from schemas import (
    AnalyzeRequest, AnalyzeResponse, TemplatesResponse, TemplateItem,
    SummaryResponse, ServicesResponse, MessageResponse,
)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("spell_api")

app = FastAPI(title="Spell 日志分析服务", version="1.0")

# 全局分析器（常驻 Spell 实例 = 论文中的在线流式解析器）
analyzer = LogAnalyzer(
    build_source(
        settings.log_source,
        api_url=settings.log_api_url,
        api_token=settings.log_api_token,
        file_path=settings.log_file,
        services=settings.log_services,
    )
)

# 并发控制（问题 #3）：原「整把大锁」会让任何并发 /analyze 直接 409，多租户不可用。
# 改为「按服务单元签名分桶」的锁：不同服务的分析可并行；同一服务的重复调用若正在
# 进行则返回 409（并发喂入同一解析器仍属误用），非阻塞的并发友好。
_feed_locks: dict = {}
_feed_locks_guard = threading.Lock()


def _lock_for(services) -> threading.Lock:
    key = ",".join(sorted(services)) if services else "*"
    with _feed_locks_guard:
        lock = _feed_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _feed_locks[key] = lock
        return lock


# ==========================================================================
# 对外核心接口（业务使用）
# ==========================================================================

@app.post("/analyze", response_model=AnalyzeResponse)
def analyze(req: AnalyzeRequest):
    """核心对外接口：分析指定服务单元在给定时间窗内的错误日志。

    处理流程（服务端内部）：
        1. 根据入参 services + start_ms/end_ms，调用日志查询接口分批拉取日志
           （真实接口由 log_client.py 封装，复杂契约可配置/可扩展）；
        2. 将日志按时间窗切片、逐页喂入 Spell 在线流式解析器；
        3. 返回本次分析结果（错误模式明细：首次/最后出现时间、占比、趋势、变化类型、错误类型、样例）。

        可重复调用，模板库持续累积（在线流式语义）。
        同一服务单元的并发喂入会被拒绝并返回 409，详见 analyzer 的并行/乱序保护。
        """
    services = req.query.split(",") if req.query else None
    lock = _lock_for(services)
    if not lock.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="同一服务单元的并发分析正在进行中，请稍后重试或串行调用。",
        )
    try:
        result = analyzer.analyze(
            services=services,
            start_ms=req.start,
            end_ms=req.end,
            batch_size=req.batch_size,
            allow_out_of_order=req.allow_out_of_order,
        )
    except (ConcurrentFeedError, OutOfOrderFeedError) as e:
        # 并行/乱序属于「误用」而非服务内部错误，用 409 表达更贴切
        raise HTTPException(status_code=409, detail=str(e))
    except Exception as e:  # noqa: BLE001
        # 记录完整 traceback 便于线上排障（原实现只 str(e) 且未记录）
        logger.exception("analyze failed: %s", e)
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        lock.release()

    return AnalyzeResponse(**result)


# ==========================================================================
# 内部 / 调试辅助接口（非对外业务契约）
# ==========================================================================

@app.get("/health", response_model=MessageResponse)
def health():
    return MessageResponse(message="ok")


@app.get("/services", response_model=ServicesResponse)
def list_services():
    """列出可查询的服务单元。"""
    return ServicesResponse(services=analyzer.services())


@app.get("/templates", response_model=TemplatesResponse)
def get_templates(
    service: Optional[str] = Query(None, description="服务单元维度过滤"),
    limit: int = Query(20, ge=1, le=200),
):
    """查看已发现的消息模板与计数。可指定 service 维度。"""
    items = analyzer.templates(service=service, limit=limit)
    return TemplatesResponse(
        service=service,
        count=len(items),
        templates=[TemplateItem(**t) for t in items],
    )


@app.get("/summary", response_model=SummaryResponse)
def get_summary():
    """全局分析概览：总量、类型数、预过滤命中分布、Top 模板。"""
    s = analyzer.summary()
    return SummaryResponse(
        total_processed=s["total_processed"],
        message_types=s["message_types"],
        prefilter_stats=s["prefilter_stats"],
        top_templates=[TemplateItem(**t) for t in s["top_templates"]],
    )


@app.post("/reset", response_model=MessageResponse)
def reset():
    """清空解析器，重新冷启动。"""
    analyzer.reset()
    return MessageResponse(message="analyzer reset")


@app.post("/save", response_model=MessageResponse)
def save_model(path: str = Query("spell_model.json")):
    """持久化模板库到 JSON。"""
    analyzer.save(path)
    return MessageResponse(message=f"saved to {path}")


@app.post("/load", response_model=MessageResponse)
def load_model(path: str = Query("spell_model.json")):
    """从 JSON 恢复模板库。"""
    analyzer.load(path)
    return MessageResponse(message=f"loaded from {path}")
