"""持久化层（对接 Spell / LogAnalyzer），支持两种后端：

  - "json"  : 原 JSON 文件持久化（全局 spell + 维度 .dim 文件），向后兼容
  - "mysql" : MySQL 8 关系存储（见 migrations/001_init_schema.sql）

字段与 Spell.to_dict() / LogAnalyzer._by_dim 的 .dim 结构 1:1 对齐：
  - spell_meta        <-> tau / total_processed / stats
  - spell_template    <-> LCSObject (seq/count/line_ids/last_seen)
  - spell_template_param <-> LCSObject.params_sample
  - analyze_run / analyze_pattern <-> LogAnalyzer.analyze 返回结果

统一接口（两个后端都实现）：
    store.save_analyzer(analyzer)          # 内存态落库（全局 + 维度分桶）
    store.load_analyzer(analyzer)          # 从库/文件恢复内存态
    store.save_analyze_result(analyzer, result)  # 落 analyze 批次
    store.top_templates(dimension, limit)  # 查询 Top 模板

工厂：
    store = build_store(mode="json", path="state.json")
    store = build_store(mode="mysql", host=..., database="spell_log")

依赖：mysql 模式需 pip install pymysql
"""
from __future__ import annotations

import hashlib
import json
import os
from typing import Dict, List, Optional

GLOBAL_DIM = "__global__"


def _sha1(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


class MysqlSpellStore:
    def __init__(self, host: str = "127.0.0.1", port: int = 3306,
                 user: str = "root", password: str = "", database: str = "spell_log",
                 charset: str = "utf8mb4"):
        import pymysql  # 延迟导入：json 模式无需安装 pymysql
        self._conn = pymysql.connect(
            host=host, port=port, user=user, password=password,
            database=database, charset=charset, autocommit=False,
        )

    # -- 内部 upsert -------------------------------------------------------

    def _upsert_meta(self, dimension: str, tau: float, total_processed: int, stats: dict) -> None:
        with self._conn.cursor() as cur:
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
            cur.execute("INSERT IGNORE INTO dim_bucket (dimension) VALUES (%s)", (dimension,))

    def _upsert_templates(self, dimension: str, types: List[dict]) -> None:
        with self._conn.cursor() as cur:
            for t in types:
                seq = t["seq"]
                template = " ".join(seq)
                thash = _sha1(template)
                line_ids = t.get("line_ids")
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
                if tid == 0:
                    cur.execute(
                        "SELECT id FROM spell_template WHERE dimension=%s AND template_hash=%s",
                        (dimension, thash),
                    )
                    tid = cur.fetchone()[0]
                # 重写参数样本（FIFO 最近5次）：先删后插，保证样本序号稳定
                cur.execute(
                    "DELETE FROM spell_template_param WHERE template_id=%s", (tid,)
                )
                for idx, params in enumerate(t.get("params_sample") or []):
                    cur.execute(
                        """INSERT INTO spell_template_param (template_id, sample_idx, params)
                           VALUES (%s, %s, %s)""",
                        (tid, idx, json.dumps(params, ensure_ascii=False)),
                    )

    # -- 落库：分析器状态 --------------------------------------------------

    def save_analyzer(self, analyzer) -> None:
        """把 LogAnalyzer 内存态（全局 spell + 维度分桶）落库。

        analyzer 需具备：spell(Spell)、_by_dim(Dict[str, Spell])。
        """
        sp = analyzer.spell
        self._upsert_meta(GLOBAL_DIM, sp.tau, sp.total_processed, sp.stats)
        self._upsert_templates(GLOBAL_DIM, sp.to_dict()["types"])
        for dim_val, sub in analyzer._by_dim.items():
            self._upsert_meta(dim_val, sub.tau, sub.total_processed, sub.stats)
            self._upsert_templates(dim_val, sub.to_dict()["types"])
        self._conn.commit()

    def load_analyzer(self, analyzer) -> None:
        """从库恢复 LogAnalyzer 内存态（覆盖 analyzer.spell 与 _by_dim）。"""
        from spell_parser import Spell
        from analyzer import _rebuild_spell

        with self._conn.cursor() as cur:
            # 全局
            cur.execute(
                "SELECT tau, total_processed, stats FROM spell_meta WHERE dimension=%s",
                (GLOBAL_DIM,),
            )
            row = cur.fetchone()
            if row:
                data = {
                    "tau": row[0], "total_processed": row[1],
                    "stats": json.loads(row[2]) if row[2] else {},
                    "types": self._load_types(cur, GLOBAL_DIM),
                }
                analyzer.spell = _rebuild_spell(data, row[0])
            # 维度分桶
            analyzer._by_dim = {}
            cur.execute("SELECT dimension FROM dim_bucket WHERE dimension<>%s", (GLOBAL_DIM,))
            for (dim_val,) in cur.fetchall():
                cur.execute(
                    "SELECT tau, total_processed, stats FROM spell_meta WHERE dimension=%s",
                    (dim_val,),
                )
                r = cur.fetchone()
                if not r:
                    continue
                d = {
                    "tau": r[0], "total_processed": r[1],
                    "stats": json.loads(r[2]) if r[2] else {},
                    "types": self._load_types(cur, dim_val),
                }
                analyzer._by_dim[dim_val] = _rebuild_spell(d, r[0])

    def _load_types(self, cur, dimension: str) -> List[dict]:
        cur.execute(
            """SELECT t.seq, t.count, t.line_ids, t.last_seen, t.id
               FROM spell_template t WHERE t.dimension=%s""",
            (dimension,),
        )
        out = []
        for seq_json, count, line_ids_json, last_seen, tid in cur.fetchall():
            # 取该模板最近参数样本
            cur.execute(
                "SELECT params FROM spell_template_param WHERE template_id=%s ORDER BY sample_idx",
                (tid,),
            )
            params = [json.loads(p[0]) for p in cur.fetchall()]
            out.append({
                "seq": json.loads(seq_json),
                "count": count,
                "line_ids": json.loads(line_ids_json) if line_ids_json else [],
                "last_seen": last_seen,
                "params_sample": params,
            })
        return out

    # -- 落库：analyze 结果 ------------------------------------------------

    def save_analyze_result(self, analyzer, result: dict,
                            by_dim: Optional[str] = None,
                            start_ms: Optional[int] = None,
                            end_ms: Optional[int] = None) -> int:
        """把一次 analyze 返回结果落库，返回 run_id。"""
        with self._conn.cursor() as cur:
            cur.execute(
                """INSERT INTO analyze_run
                   (services, start_ms, end_ms, by_dim, newly_processed,
                    total_processed, window_total, message_types)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    json.dumps(result.get("services", []), ensure_ascii=False),
                    start_ms, end_ms, by_dim,
                    result.get("newly_processed", 0),
                    result.get("total_processed", 0),
                    result.get("window_total", 0),
                    result.get("message_types", 0),
                ),
            )
            run_id = cur.lastrowid
            for rank, pat in enumerate(result.get("patterns", [])):
                cur.execute(
                    """INSERT INTO analyze_pattern
                       (run_id, rank, template, template_hash, count, ratio,
                        first_seen_ms, last_seen_ms, trend, change_type, error_type, sample)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                    (
                        run_id, rank, pat["template"], _sha1(pat["template"]),
                        pat.get("count", 0), pat.get("ratio", 0),
                        pat.get("first_seen_ms"), pat.get("last_seen_ms"),
                        json.dumps(pat.get("trend", []), ensure_ascii=False),
                        pat.get("change_type"), pat.get("error_type"), pat.get("sample"),
                    ),
                )
            self._conn.commit()
            return run_id

    # -- 查询辅助 ----------------------------------------------------------

    def top_templates(self, dimension: str = GLOBAL_DIM, limit: int = 20) -> List[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT template, count FROM spell_template WHERE dimension=%s "
                "ORDER BY count DESC LIMIT %s",
                (dimension, limit),
            )
            return [{"template": t, "count": c} for t, c in cur.fetchall()]

    def close(self) -> None:
        self._conn.close()


# ---------------------------------------------------------------------------
# JSON 后端：复用原 analyzer.save/load 的文件结构（全局 + .dim 维度）
# ---------------------------------------------------------------------------

class JsonSpellStore:
    """基于 JSON 文件的持久化后端（向后兼容原 file 模式）。

    文件布局与 LogAnalyzer.save/load 完全一致：
      - {path}            : 全局 spell（Spell.to_dict）
      - {path}.dim        : 维度分桶 {dim_val: Spell.to_dict}
      - {path}.runs.json  : 历次 analyze 结果（追加写，便于审计）
    """

    def __init__(self, path: str = "spell_state.json"):
        self.path = path
        self.dim_path = path + ".dim"
        self.runs_path = path + ".runs.json"

    # -- 分析器状态 --------------------------------------------------------

    def save_analyzer(self, analyzer) -> None:
        analyzer.spell.save(self.path)
        with open(self.dim_path, "w", encoding="utf-8") as f:
            json.dump(
                {k: v.to_dict() for k, v in analyzer._by_dim.items()},
                f, ensure_ascii=False, indent=2,
            )

    def load_analyzer(self, analyzer) -> None:
        from spell_parser import Spell
        from analyzer import _rebuild_spell
        analyzer.spell = Spell.load(self.path)
        analyzer._by_dim = {}
        try:
            with open(self.dim_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for k, d in raw.items():
                analyzer._by_dim[k] = _rebuild_spell(d, analyzer.spell.tau)
        except FileNotFoundError:
            pass

    # -- analyze 结果 ------------------------------------------------------

    def save_analyze_result(self, analyzer, result: dict,
                            by_dim: Optional[str] = None,
                            start_ms: Optional[int] = None,
                            end_ms: Optional[int] = None) -> int:
        """追加写入一次 analyze 结果，返回该 run 的序号（run_id）。"""
        runs = []
        if os.path.exists(self.runs_path):
            try:
                with open(self.runs_path, "r", encoding="utf-8") as f:
                    runs = json.load(f)
            except Exception:
                runs = []
        run_id = len(runs)
        runs.append({
            "run_id": run_id,
            "by_dim": by_dim,
            "start_ms": start_ms,
            "end_ms": end_ms,
            "result": result,
        })
        with open(self.runs_path, "w", encoding="utf-8") as f:
            json.dump(runs, f, ensure_ascii=False, indent=2)
        return run_id

    # -- 查询辅助 ----------------------------------------------------------

    def top_templates(self, dimension: str = GLOBAL_DIM, limit: int = 20) -> List[dict]:
        from spell_parser import Spell
        spell = None
        if dimension == GLOBAL_DIM:
            if os.path.exists(self.path):
                spell = Spell.load(self.path)
        else:
            if os.path.exists(self.dim_path):
                try:
                    with open(self.dim_path, "r", encoding="utf-8") as f:
                        raw = json.load(f)
                    d = raw.get(dimension)
                    if d:
                        spell = _rebuild_spell(d, 0.5)
                except Exception:
                    spell = None
        if spell is None:
            return []
        return [{"template": t, "count": c} for t, c in spell.templates()[:limit]]

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# 工厂：根据 mode 选择后端
# ---------------------------------------------------------------------------

def build_store(mode: str = "json", path: str = "spell_state.json", **mysql_kwargs) -> object:
    """构造持久化后端。

    mode="json"  -> JsonSpellStore(path=...)
    mode="mysql" -> MysqlSpellStore(**mysql_kwargs)  # host/user/password/database/port/charset
    """
    if mode == "json":
        return JsonSpellStore(path=path)
    if mode == "mysql":
        return MysqlSpellStore(**mysql_kwargs)
    raise ValueError(f"未知持久化模式: {mode!r}（支持 'json' / 'mysql'）")


def build_store_from_settings(settings, **overrides) -> object:
    """从 config.Settings 构造持久化后端。

    overrides 可覆盖任意字段（如 path / mode / mysql_*）。
    环境变量 / config.yaml 已通过 Settings 解析，此处直接取字段。
    """
    mode = overrides.pop("mode", getattr(settings, "persistence_mode", "json"))
    path = overrides.pop("path", getattr(settings, "persistence_path", "spell_state.json"))
    if mode == "json":
        return JsonSpellStore(path=path)
    if mode == "mysql":
        mysql_kwargs = dict(
            host=overrides.pop("mysql_host", getattr(settings, "mysql_host", "127.0.0.1")),
            port=int(overrides.pop("mysql_port", getattr(settings, "mysql_port", "3306"))),
            user=overrides.pop("mysql_user", getattr(settings, "mysql_user", "root")),
            password=overrides.pop("mysql_password", getattr(settings, "mysql_password", "")),
            database=overrides.pop("mysql_database", getattr(settings, "mysql_database", "spell_log")),
        )
        mysql_kwargs.update(overrides)  # 允许 charset 等额外参数
        return MysqlSpellStore(**mysql_kwargs)
    raise ValueError(f"未知持久化模式: {mode!r}（支持 'json' / 'mysql'）")
