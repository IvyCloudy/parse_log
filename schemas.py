"""FastAPI 请求/响应模型。"""

from __future__ import annotations

from typing import List, Optional

from pydantic import BaseModel, Field


class AnalyzeRequest(BaseModel):
    start: int = Field(1787278400000, description="起始时间(ms, 含)")
    end: int = Field(1787282000000, description="截止时间(ms, 含)")
    query: Optional[str] = Field(
        None, description="查询条件：服务单元列表，逗号分隔(如 'order-unit-1,pay-unit-1')；"
                          "省略则查询全部可发现的服务单元")
    batch_size: int = Field(50, description="单次接口返回条数(可设>=10000)")
    allow_out_of_order: bool = Field(
        False, description="允许乱序喂入(仅重新训练模板库时使用)；默认开启流式递增保护")


class TrendPoint(BaseModel):
    bucket_start_ms: int = Field(description="该趋势桶的起始时间(ms)")
    count: int = Field(description="该时间桶内的日志条数")


class PatternRecord(BaseModel):
    """单条错误模式的分析结果。"""
    template: str = Field(description="日志模式（模板，可变参数标为 *）")
    count: int = Field(description="模式日志量（命中条数）")
    ratio: float = Field(description="模式日志量占总量的比例(%)")
    first_seen_ms: int = Field(description="首次出现时间(ms)")
    last_seen_ms: int = Field(description="最后出现时间(ms)")
    trend: List[TrendPoint] = Field(description="数据趋势：按时间均分10桶的计数序列")
    change_type: str = Field(description="数量变化类型：持续存在 / 新增")
    error_type: str = Field(description="错误类型：网络异常 / 业务异常 / 平台问题")
    sample: str = Field(description="样例日志（原始 message）")


class AnalyzeResponse(BaseModel):
    services: List[str] = Field(description="实际分析的服务单元")
    newly_processed: int = Field(description="本批新增处理条数")
    total_processed: int = Field(description="累计处理条数")
    window_total: int = Field(description="本次时间窗内处理的日志总量")
    message_types: int = Field(description="发现的错误模式（模板）数")
    patterns: List[PatternRecord] = Field(description="错误模式明细列表（按数量降序）")


class TemplateItem(BaseModel):
    template: str
    count: int


class TemplatesResponse(BaseModel):
    service: Optional[str] = None
    count: int
    templates: List[TemplateItem]


class SummaryResponse(BaseModel):
    total_processed: int
    message_types: int
    prefilter_stats: dict
    top_templates: List[TemplateItem]


class ServicesResponse(BaseModel):
    services: List[str]


class MessageResponse(BaseModel):
    message: str
