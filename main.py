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

    # 持久化（config.yaml 的 persistence.mode 决定 json/mysql）
    python main.py --file logs.jsonl --save spell_state.json            # json 模式保存
    python main.py --serve --persist-mode mysql                        # 服务按 mysql 落库
    python main.py --migrate --mysql-host db --mysql-database spell_log # 建表
    python main.py --migrate --import-json spell_state.json             # 建表+灌旧 JSON

说明: CLI 参数显式优先；数据源默认类型与持久化模式也可在 config.yaml 中配置（详见 config.py）。
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


def _apply_persist_env(args):
    """把 CLI 的持久化参数转成环境变量，供 api.py / config.py 读取。"""
    if args.persist_mode:
        os.environ["PERSISTENCE_MODE"] = args.persist_mode
    if args.persist_path:
        os.environ["PERSISTENCE_PATH"] = args.persist_path
    # mysql 连接参数（可选，便于 --serve 模式下覆盖 config.yaml）
    if args.mysql_host:
        os.environ["MYSQL_HOST"] = args.mysql_host
    if args.mysql_port:
        os.environ["MYSQL_PORT"] = str(args.mysql_port)
    if args.mysql_user:
        os.environ["MYSQL_USER"] = args.mysql_user
    if args.mysql_password is not None:
        os.environ["MYSQL_PASSWORD"] = args.mysql_password
    if args.mysql_database:
        os.environ["MYSQL_DATABASE"] = args.mysql_database


def run_migration_cmd(args):
    """执行 MySQL 建表（可选灌入旧 JSON）。

    连接参数优先级：CLI --mysql-* > 已注入的环境变量（_apply_persist_env）> 环境变量默认值。
    """
    # 通过环境变量统一来源，再交给 run_migration.parse_args（它也读 MYSQL_* 默认值）
    _apply_persist_env(args)
    from migrations import run_migration
    m_args = run_migration.parse_args([])
    # 用 CLI 显式值覆盖（环境变量已覆盖默认值，这里把命令行值再盖一层）
    if args.mysql_host:
        m_args.host = args.mysql_host
    if args.mysql_port:
        m_args.port = args.mysql_port
    if args.mysql_user:
        m_args.user = args.mysql_user
    if args.mysql_password is not None:
        m_args.password = args.mysql_password
    if args.mysql_database:
        m_args.database = args.mysql_database
    if args.import_json:
        m_args.import_json = args.import_json
    print(f"[migrate] host={m_args.host} db={m_args.database} "
          f"import={m_args.import_json or '建表仅'}")
    try:
        run_migration.run(m_args)
    except SystemExit:
        raise
    except Exception as e:  # noqa: BLE001
        raise SystemExit(f"迁移失败: {e}")


def start_server(args):
    """启动 FastAPI 服务（内部用 uvicorn 运行 api:app）。"""
    try:
        import uvicorn
    except ImportError:
        raise SystemExit("错误: 启动服务需安装 uvicorn（pip install uvicorn）")

    # 将 CLI 数据源参数注入环境变量，api.py 启动时据此构建数据源
    _apply_source_env(args)
    # 将 CLI 持久化参数注入环境变量，api.py 启动时据此选择 json/mysql 后端
    _apply_persist_env(args)

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
    parser.add_argument("--save", help="把模板库保存到该路径（模式由 --persist-mode 决定）")
    parser.add_argument("--load", help="从指定路径恢复已有模板库（模式由 --persist-mode 决定）")
    # 持久化模式（json / mysql）
    parser.add_argument("--persist-mode", default=None,
                        choices=["json", "mysql"],
                        help="持久化模式：json=文件（默认），mysql=关系库；"
                             "缺省则从 config.yaml 的 persistence.mode 读取")
    parser.add_argument("--persist-path", default=None,
                        help="持久化路径/标识（json=文件路径，mysql 忽略）；缺省读 config.yaml")
    parser.add_argument("--mysql-host", default=None, help="MySQL host（mysql 模式，覆盖配置）")
    parser.add_argument("--mysql-port", type=int, default=None, help="MySQL port")
    parser.add_argument("--mysql-user", default=None, help="MySQL user")
    parser.add_argument("--mysql-password", default=None, help="MySQL password")
    parser.add_argument("--mysql-database", default=None, help="MySQL database")
    # 数据库迁移（建表 / 灌旧 JSON）：仅 mysql 模式有意义
    parser.add_argument("--migrate", action="store_true",
                        help="执行 MySQL 建表（001_init_schema.sql）；可结合 --import-json 灌入旧数据")
    parser.add_argument("--import-json", default=None,
                        help="--migrate 时可选：把该 JSON 持久化文件灌入 MySQL（同名 .dim 一并导入）")
    # 服务启动
    parser.add_argument("--serve", action="store_true", help="启动 FastAPI 服务（而非一次性 CLI 分析）")
    parser.add_argument("--host", default="0.0.0.0", help="服务监听地址（--serve 时）")
    parser.add_argument("--port", type=int, default=8000, help="服务监听端口（--serve 时）")
    parser.add_argument("--reload", action="store_true", help="开启热重载（--serve 时，仅开发用）")
    args = parser.parse_args()

    if args.migrate:
        run_migration_cmd(args)
        return

    if args.serve:
        start_server(args)
        return

    source = build_source(args)
    analyzer = LogAnalyzer(source, tau=0.5)
    if args.load:
        analyzer.load(args.load, mode=args.persist_mode)

    services: Optional[List[str]] = args.services.split(",") if args.services else None
    overview = analyzer.analyze(
        services=services, start_ms=args.start, end_ms=args.end,
        batch_size=args.batch, by_dim=args.by,
        persist_mode=args.persist_mode, persist_path=args.persist_path or args.save,
    )
    print(f"[分析概览] 本批新增 {overview['newly_processed']} 条, "
          f"累计 {overview['total_processed']} 条, 模板 {overview['message_types']} 类")

    print(analyzer.summary())

    if args.save:
        analyzer.save(args.save, mode=args.persist_mode)
        print(f"\n[已保存模板库] -> {args.save} (mode={args.persist_mode or 'config'})")


if __name__ == "__main__":
    main()
