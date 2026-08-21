# 迁移到 MySQL 持久化

把原 JSON 文件持久化（`analyzer.save/load`）迁移到 MySQL 8。

## 文件
- `001_init_schema.sql` — 建库建表 DDL（幂等，可重复执行）
- `run_migration.py` — 可执行迁移脚本：建表 + 可选灌入旧 JSON
- `store.py`（项目根） — 应用层持久化封装，对接 `LogAnalyzer`

## 表结构（7 张）
| 表 | 对应 JSON 字段 |
|---|---|
| `spell_meta` | `tau` / `total_processed` / `stats`（每 dimension 一行） |
| `dim_bucket` | 维度枚举（含 `__global__`） |
| `spell_template` | `LCSObject`（seq/count/line_ids/last_seen） |
| `spell_template_param` | `params_sample`（最近 5 次） |
| `analyze_run` | analyze 返回顶层字段 |
| `analyze_pattern` | `AnalyzeResponse.patterns` 明细 |

`dimension='__global__'` 代表主 spell；其余为 `by_dim` 维度分桶。

## 执行

### 方式 A：通过 main.py（推荐，配置统一走 config.yaml）
```bash
pip install pymysql

# 仅建表（连接取 --mysql-* 或 config.yaml 的 persistence.mysql）
python main.py --migrate --mysql-host 127.0.0.1 --mysql-user root --mysql-database spell_log

# 建表 + 灌入历史 JSON（LogAnalyzer.save 产出，同名 .dim 一并导入）
python main.py --migrate --import-json spell_state.json --mysql-host 127.0.0.1
```

### 方式 B：直接跑脚本（参数/环境变量驱动）
```bash
# 1. 仅建表
python migrations/run_migration.py \
  --host 127.0.0.1 --port 3306 --user root --password 'xxx' --database spell_log

# 2. 建表 + 灌入历史 JSON
python migrations/run_migration.py \
  --host 127.0.0.1 --port 3306 --user root --password 'xxx' --database spell_log \
  --import-json path/to/spell_state.json
```

连接参数也可用环境变量：`MYSQL_HOST / MYSQL_PORT / MYSQL_USER / MYSQL_PASSWORD / MYSQL_DATABASE`
（main.py 的 `--migrate` 还会读取这些环境变量与 config.yaml，优先级：CLI --mysql-* > 环境变量 > 默认值）。

## 应用接入
持久化模式由 `config.yaml` 的 `persistence.mode`（`json` / `mysql`）决定，CLI 可用
`--persist-mode` 覆盖，环境变量用 `PERSISTENCE_MODE` 覆盖。

```bash
# json 模式（默认）
python main.py --file logs.jsonl --save spell_state.json
python main.py --serve                       # 启动时自动按配置 load/落库

# mysql 模式（连接取 --mysql-* 或 config.yaml 的 persistence.mysql）
python main.py --serve --persist-mode mysql
```

代码层（无需关心 mode）：
```python
from config import settings
from analyzer import LogAnalyzer

analyzer.save()   # 按 settings.persistence_mode 落库（json/mysql）
analyzer.load()   # 按 settings.persistence_mode 恢复
analyzer.analyze(..., persist_mode=settings.persistence_mode)  # analyze 结果落库
```

## 移植注意
- 当前 DDL 为 MySQL 8（`utf8mb4_0900_ai_ci` / `JSON` / `ON DUPLICATE KEY UPDATE`）。
- PostgreSQL：把 `ON DUPLICATE KEY UPDATE` 改为 `ON CONFLICT (dimension,template_hash) DO UPDATE`，
  `JSON` 列改为 `JSONB`，`CURRENT_TIMESTAMP(3)` 不变。
- SQLite：去掉 `ENGINE=`/`AUTO_INCREMENT`，`JSON` 用 `TEXT`，`ON CONFLICT` 语法同 PG 思路。
