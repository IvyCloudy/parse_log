#!/usr/bin/env python3
"""Spell 日志分析服务 — MySQL 迁移执行脚本（可执行）。

用途：
  1. 连接 MySQL，执行 001_init_schema.sql 建库建表。
  2. 可选：把历史 JSON 持久化文件（全局 spell + 维度 .dim）灌入 MySQL，
     实现从文件持久化到关系存储的平滑迁移。

依赖：
  pip install pymysql

用法：
  # 仅建表（幂等，可重复执行）
  python migrations/run_migration.py \
      --host 127.0.0.1 --port 3306 --user root --password 'xxx' \
      --database spell_log

  # 建表 + 灌入旧数据
  python migrations/run_migration.py \
      --host 127.0.0.1 --port 3306 --user root --password 'xxx' \
      --database spell_log \
      --import-json path/to/spell_state.json

说明：
  - 连接参数也可用环境变量覆盖：MYSQL_HOST / MYSQL_PORT / MYSQL_USER /
    MYSQL_PASSWORD / MYSQL_DATABASE。
  - --import-json 指向的应是 LogAnalyzer.save 产出的全局 spell JSON；同一目录下的
    "<name>.dim" 文件会被一并导入（维度分桶）。
  - 表已带 IF NOT EXISTS / UNIQUE KEY，重复执行安全（不会丢数据，count 累加）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys


SQL_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "001_init_schema.sql")

GLOBAL_DIM = "__global__"


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def connect(args) -> "pymysql.connections.Connection":
    import pymysql  # 延迟导入：未安装时仅在实际连接才报错
    return pymysql.connect(
        host=args.host,
        port=args.port,
        user=args.user,
        password=args.password,
        database=args.database if _db_exists(args) else None,
        charset="utf8mb4",
        autocommit=False,
    )


def _db_exists(args) -> bool:
    try:
        c = pymysql.connect(
            host=args.host, port=args.port, user=args.user,
            password=args.password, charset="utf8mb4",
        )
        with c.cursor() as cur:
            cur.execute(
                "SELECT SCHEMA_NAME FROM INFORMATION_SCHEMA.SCHEMATA "
                "WHERE SCHEMA_NAME=%s",
                (args.database,),
            )
            exists = cur.fetchone() is not None
        c.close()
        return exists
    except Exception:
        return False


def run_schema(conn: "pymysql.connections.Connection") -> None:
    """执行建表 SQL（按分号切分逐条执行，容错 IF NOT EXISTS）。"""
    with open(SQL_FILE, "r", encoding="utf-8") as f:
        sql = f.read()
    # 去掉 USE 语句由连接 database 控制；其余按 ';' 切分
    statements = [s.strip() for s in sql.split(";") if s.strip() and not s.strip().upper().startswith("USE")]
    with conn.cursor() as cur:
        for stmt in statements:
            cur.execute(stmt)
    conn.commit()
    print(f"[ok] schema applied: {len(statements)} statements")


def _upsert_meta(conn, dimension: str, tau: float, total_processed: int, stats: dict) -> None:
    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO spell_meta (dimension, tau, total_processed, stats)
               VALUES (%s, %s, %s, %s)
               ON DUPLICATE KEY UPDATE
                 tau=VALUES(tau),
                 total_processed=GREATEST(total_processed, VALUES(total_processed)),
                 stats=VALUES(stats),
                 updated_at=CURRENT_TIMESTAMP(3)""",
            (dimension, tau, total_processed, json.dumps(stats, ensure_ascii=False)),
        )
        cur.execute(
            "INSERT IGNORE INTO dim_bucket (dimension) VALUES (%s)", (dimension,)
        )


def _upsert_templates(conn, dimension: str, types: list) -> None:
    for t in types:
        seq = t["seq"]
        template = " ".join(seq)
        thash = _sha1(template)
        line_ids = t.get("line_ids")
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO spell_template
                   (dimension, template_hash, template, seq, count, line_ids, last_seen)
                   VALUES (%s, %s, %s, %s, %s, %s, %s)
                   ON DUPLICATE KEY UPDATE
                     count=count+VALUES(count),
                     line_ids=VALUES(line_ids),
                     last_seen=GREATEST(COALESCE(last_seen,0), COALESCE(VALUES(last_seen),0)),
                     updated_at=CURRENT_TIMESTAMP(3)""",
                (
                    dimension, thash, template, json.dumps(seq, ensure_ascii=False),
                    t.get("count", 0),
                    json.dumps(line_ids, ensure_ascii=False) if line_ids is not None else None,
                    t.get("last_seen"),
                ),
            )
            tid = cur.lastrowid
            if tid == 0:  # 命中已存在行，取 id
                cur.execute(
                    "SELECT id FROM spell_template WHERE dimension=%s AND template_hash=%s",
                    (dimension, thash),
                )
                tid = cur.fetchone()[0]
            # 参数样本（最近5次）
            for idx, params in enumerate(t.get("params_sample") or []):
                cur.execute(
                    """INSERT INTO spell_template_param (template_id, sample_idx, params)
                       VALUES (%s, %s, %s)""",
                    (tid, idx, json.dumps(params, ensure_ascii=False)),
                )


def import_json(conn, json_path: str) -> None:
    """把 LogAnalyzer.save 产出的全局 JSON + 同名 .dim 维度文件灌入 MySQL。"""
    base = json_path
    with open(base, "r", encoding="utf-8") as f:
        global_data = json.load(f)

    # 全局主解析器
    _upsert_meta(
        conn, GLOBAL_DIM,
        global_data.get("tau", 0.5),
        global_data.get("total_processed", 0),
        global_data.get("stats", {}),
    )
    _upsert_templates(conn, GLOBAL_DIM, global_data.get("types", []))
    print(f"[ok] imported global spell from {os.path.basename(base)}")

    # 维度分桶（<name>.dim）
    dim_path = base + ".dim"
    if os.path.exists(dim_path):
        with open(dim_path, "r", encoding="utf-8") as f:
            dim_raw = json.load(f)
        for dim_val, d in dim_raw.items():
            _upsert_meta(
                conn, dim_val, d.get("tau", 0.5),
                d.get("total_processed", 0), d.get("stats", {}),
            )
            _upsert_templates(conn, dim_val, d.get("types", []))
        print(f"[ok] imported {len(dim_raw)} dimension buckets from {os.path.basename(dim_path)}")

    conn.commit()


def parse_args(argv: Optional[list] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Spell MySQL migration runner")
    p.add_argument("--host", default=os.getenv("MYSQL_HOST", "127.0.0.1"))
    p.add_argument("--port", type=int, default=int(os.getenv("MYSQL_PORT", "3306")))
    p.add_argument("--user", default=os.getenv("MYSQL_USER", "root"))
    p.add_argument("--password", default=os.getenv("MYSQL_PASSWORD", ""))
    p.add_argument("--database", default=os.getenv("MYSQL_DATABASE", "spell_log"))
    p.add_argument("--import-json", default=None,
                   help="可选：旧 JSON 持久化文件路径，灌入 MySQL 完成迁移")
    return p.parse_args(argv)


def run(args: argparse.Namespace) -> None:
    conn = connect(args)
    try:
        run_schema(conn)
        if args.import_json:
            if not os.path.exists(args.import_json):
                sys.stderr.write(f"--import-json 文件不存在: {args.import_json}\n")
                sys.exit(1)
            import_json(conn, args.import_json)
        print("[done] migration complete.")
    finally:
        conn.close()


def main() -> None:
    run(parse_args())


if __name__ == "__main__":
    main()
