"""Spell 错误日志流式分析 —— CLI 演示入口。

复用 analyzer.LogAnalyzer 与 data_sources，避免与 FastAPI 层重复。

接口返回结构见 log-format.json：data.logDatas[] 每条含
message / level / logDate / appName / logger 等字段。

用法:
    python main.py                      # 内置 mock（服务单元+起止时间），单次可返回万条
    python main.py --file logs.jsonl    # 本地 JSONL（每行一条 logData）
    python main.py --real --url <API>   # 对接真实 HTTP 接口（需 pip install requests）
    python main.py --by appName         # 按 appName 维度分组统计
    python main.py --services gw-app,db-app --start <ms> --end <ms>
    python main.py --serve                       # 启动 FastAPI 服务（同 uvicorn api:app）
    python main.py --serve --real --url <API>    # 以真实接口为数据源启动服务
    python main.py --serve --file demo/test_logs.jsonl --port 8000

说明: CLI 参数显式优先；数据源默认类型也可在 config.yaml 中配置（详见 config.py）。
"""

from __future__ import annotations

import argparse
import os
from typing import List, Optional

from data_sources import build_source as _build_source_from_config
from analyzer import LogAnalyzer


def _cli_source_type(args) -> str:
    if args.file:
        return "file"
    if args.real:
        return "http"
    return "mock"


def build_source(args):
    """复用 data_sources.build_source 统一入口（避免与 api.py 重复构建逻辑，问题 #12）。"""
    return _build_source_from_config(
        _cli_source_type(args),
        api_url=args.url or "",
        api_token=args.token or "",
        file_path=args.file or "",
        services=args.services,
        batch_size=args.batch,
        mock_total=args.total,
    )


def _apply_source_env(args):
    """把 CLI 的数据源参数转成环境变量，供 api.py / config.py 读取。"""
    if args.real:
        os.environ["LOG_SOURCE"] = "http"
        if args.url:
            os.environ["LOG_API_URL"] = args.url
        if args.token:
            os.environ["LOG_API_TOKEN"] = args.token
    elif args.file:
        os.environ["LOG_SOURCE"] = "file"
        os.environ["LOG_FILE"] = args.file
    else:
        os.environ["LOG_SOURCE"] = "mock"
    if args.services:
        os.environ["LOG_SERVICES"] = args.services


def start_server(args):
    """启动 FastAPI 服务（内部用 uvicorn 运行 api:app）。"""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("错误: 启动服务需安装 uvicorn（pip install uvicorn）")

    # 将 CLI 数据源参数注入环境变量，api.py 启动时据此构建数据源
    _apply_source_env(args)

    print(f"[启动服务] http://{args.host}:{args.port}  (数据源={os.environ.get('LOG_SOURCE')})")
    uvicorn.run(
        "api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
    )


def main():
    parser = argparse.ArgumentParser(description="Spell 错误日志流式分析（服务单元+起止时间）")
    parser.add_argument("--real", action="store_true", help="对接真实 HTTP 接口")
    parser.add_argument("--url", help="真实接口 base url")
    parser.add_argument("--token", default="", help="接口鉴权 token（可选）")
    parser.add_argument("--file", help="从本地 JSONL 文件读取(每行一条 logData)")
    parser.add_argument("--batch", type=int, default=50, help="单次接口返回条数(可设>=10000)")
    parser.add_argument("--total", type=int, default=12000, help="mock 生成日志总量(>=10000)")
    parser.add_argument("--services", default=None,
                        help="服务单元列表，逗号分隔；省略则自动发现")
    parser.add_argument("--by", default=None,
                        help="按该字段维度分组统计（如 appName），为每个维度维护独立模板库")
    parser.add_argument("--start", type=int, default=1787278400000,
                        help="查询起始时间(ms)")
    parser.add_argument("--end", type=int, default=1787282000000,
                        help="查询截止时间(ms)")
    parser.add_argument("--save", help="把模板库保存到该 JSON 路径")
    parser.add_argument("--load", help="从 JSON 恢复已有模板库")
    # 服务启动
    parser.add_argument("--serve", action="store_true", help="启动 FastAPI 服务（而非一次性 CLI 分析）")
    parser.add_argument("--host", default="0.0.0.0", help="服务监听地址（--serve 时）")
    parser.add_argument("--port", type=int, default=8000, help="服务监听端口（--serve 时）")
    parser.add_argument("--reload", action="store_true", help="开启热重载（--serve 时，仅开发用）")
    args = parser.parse_args()

    if args.serve:
        start_server(args)
        return

    source = build_source(args)
    analyzer = LogAnalyzer(source, tau=0.5)
    if args.load:
        analyzer.load(args.load)

    services: Optional[List[str]] = args.services.split(",") if args.services else None
    overview = analyzer.analyze(
        services=services, start_ms=args.start, end_ms=args.end,
        batch_size=args.batch, by_dim=args.by,
    )
    print(f"[分析概览] 本批新增 {overview['newly_processed']} 条, "
          f"累计 {overview['total_processed']} 条, 模板 {overview['message_types']} 类")

    print(analyzer.summary())

    if args.save:
        analyzer.save(args.save)
        print(f"\n[已保存模板库] -> {args.save}")


if __name__ == "__main__":
    main()
