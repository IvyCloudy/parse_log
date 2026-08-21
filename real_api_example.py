"""对接真实「错误日志查询接口」的示例。

接口返回结构见 log-format.json：
    data.logDatas[] 中每条含 message / level / logDate / appName / logger 等字段。

本示例直接复用项目正式的组件，避免与正式代码重复、也避免引用已不存在的符号：
    - 数据源用 data_sources.HttpSource（底层由 log_client.LogClient 封装真实 HTTP 契约）
    - 分析用 analyzer.LogAnalyzer（支持 by_dim 维度分桶，且自带在线流式 + 持久化）
    - 时间解析用 data_sources.log_date_to_ms（保留毫秒与时区，见问题 #9 修复）

用法:
    python real_api_example.py
    LOG_API_URL=https://...  python real_api_example.py
"""

from __future__ import annotations

import os

from data_sources import HttpSource, log_date_to_ms
from analyzer import LogAnalyzer


def run(base_url: str, token: str = "", services: str = None,
        by_dim: str = "appName", start_ms: int = 1787278400000,
        end_ms: int = 1787282000000, batch_size: int = 500,
        model_path: str = "spell_model.json"):
    """用真实 HTTP 接口跑一次在线流式解析，并持久化模板库。

    by_dim: 按该字段维度分桶（如 appName），为每个维度维护独立模板库。
    时间解析统一走 log_date_to_ms，避免重复实现导致精度丢失。
    """
    source = HttpSource(base_url, token, batch_size=batch_size,
                        services=services.split(",") if services else None)
    analyzer = LogAnalyzer(source, tau=0.5)
    if os.path.exists(model_path):
        analyzer.load(model_path)

    overview = analyzer.analyze(
        services=services.split(",") if services else None,
        start_ms=start_ms, end_ms=end_ms, batch_size=batch_size, by_dim=by_dim,
    )
    print(f"[概览] 新增 {overview['newly_processed']} 条, "
          f"累计 {overview['total_processed']} 条, 模板 {overview['message_types']} 类")

    if by_dim and analyzer._by_dim:
        for key, sub in analyzer._by_dim.items():
            print(f"\n===== {by_dim}={key} =====")
            print(sub.summary())
    else:
        print(analyzer.summary())

    analyzer.save(model_path)
    return analyzer


if __name__ == "__main__":
    run(
        os.environ.get("LOG_API_URL", "https://your-log-service.example.com/api/errors"),
        token=os.environ.get("LOG_API_TOKEN", ""),
        by_dim="appName",
    )
