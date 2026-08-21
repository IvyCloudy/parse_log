# Spell 日志分析服务 — 接口说明文档

基于 Spell（LCS 流式日志解析）论文思路，对"通过接口分批查询的错误日志"做在线流式解析。
本服务把解析能力以 HTTP 接口对外提供。

- 服务标题：`Spell 日志分析服务`，版本 `1.0`
- 框架：FastAPI + Uvicorn
- 在线文档（Swagger）：`http://<host>:<port>/docs`
- OpenAPI JSON：`http://<host>:<port>/openapi.json`

---

## 1. 启动与数据源配置

配置统一由 `config.py` 加载，**优先级：环境变量 > config.yaml > 默认值**。
启动推荐用统一入口 `main.py --serve`（内部即 uvicorn 运行 `api:app`，CLI 数据源参数自动透传为环境变量）：

```bash
# 演示 / 文件 / 真实接口三种数据源
python main.py --serve --port 8000                       # mock
python main.py --serve --file demo/test_logs.jsonl       # file
python main.py --serve --real --url https://your-api/errors  # http
```

等价地，也可直接用 uvicorn 启动（需自行配置环境变量）：
```bash
LOG_SOURCE=file LOG_FILE=demo/test_logs.jsonl uvicorn api:app --port 8000
```

### 配置文件 config.yaml

项目根目录 `config.yaml` 可配置数据源与解析参数（详见文件内注释）。示例：

```yaml
log_source: file            # mock / file / http

data_source:
  api_url: "https://your-api/errors"
  api_token: ""
  file: "demo/test_logs.jsonl"
  services: ""              # 逗号分隔的 serviceUinitId；留空=服务发现

parser:
  tau: 0.5
```

可通过环境变量 `CONFIG_FILE` 指定其他路径。

### 环境变量（覆盖配置文件，兼容历史命名）

| 环境变量 | 说明 | 必填 | 默认值 |
|---|---|---|---|
| `CONFIG_FILE` | 自定义配置文件路径 | 否 | 项目根 `config.yaml` |
| `LOG_SOURCE` | 数据源类型：`mock` / `file` / `http` | 否 | `http` |
| `LOG_FILE` | `LOG_SOURCE=file` 时，本地 JSONL 文件路径（每行一条 logData） | file 模式必填 | 空 |
| `LOG_API_URL` | `LOG_SOURCE=http` 时，真实日志接口 base url | http 模式必填 | 空 |
| `LOG_API_TOKEN` | http 模式鉴权 token（可选，注入 `Authorization: Bearer`） | 否 | 空 |
| `LOG_SERVICES` | http 模式指定服务单元列表（逗号分隔）；省略则依赖服务发现 | 否 | 空 |

### 三种数据源

- **`mock`（仅本地开发/演示）**：`demo/mock_source.py` 生成不同类型数据库操作的 Java 层报错日志，单接口可返回万条以上。**不进入生产代码路径**，仅在显式选择 mock 时延迟导入。
  ```bash
  python main.py --serve --port 8000
  # 或：LOG_SOURCE=mock uvicorn api:app --port 8000
  ```
- **`file（本地文件）**：读 JSONL，每行一条 `logData`（结构见 `log-format.json`）。
  ```bash
  python main.py --serve --file demo/test_logs.jsonl --port 8000
  # 或写入 config.yaml: log_source: file / data_source.file: demo/test_logs.jsonl
  # 或：LOG_SOURCE=file LOG_FILE=demo/test_logs.jsonl uvicorn api:app --port 8000
  ```
- **`http`（生产）**：对接真实日志查询接口，按 `service / startTime / endTime / limit` 分页拉取。
  接口复杂性（请求方式、鉴权、响应解析、分页策略、字段映射）统一封装在 **`log_client.py`**，
  默认契约遵循 `log-format.json`；真实接口复杂时通过 `LogClientConfig` 配置或继承 `LogClient` 重写钩子，分析流程无需改动。
  ```bash
  LOG_SOURCE=http LOG_API_URL=https://your-api/errors uvicorn api:app --port 8000
  ```

### 日志数据结构（每条 logData）

与 `log-format.json` 一致，关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `logType` | string | 日志类型，如 `APP` |
| `level` | string | 级别：`ERROR` / `WARN` / `INFO` … |
| `message` | string | 日志正文（解析对象） |
| `logDate` | string | 时间戳，格式 `YYYY-MM-DDTHH:MM:SS.sssssss+0800` |
| `traceId` | string | 链路追踪 ID |
| `method` | string | 抛出位置（Java 栈） |
| `logger` | string | logger 名称 |
| `appName` | string | **应用名称**（如 `gateway-app`） |
| `serviceUinitId` | string | **服务单元 ID**（如 `gateway-unit-1`）—— 分页与分组用的「服务单元」维度；一个应用可含多个服务单元(1:N) |

> 注意：`serviceUinitId` 是"服务单元"维度（接口入参 `query`、分页都用它）；`appName` 仅表示应用名称，二者不同。

---

## 2. 通用约定

### 流式解析语义（重要）

解析器是**单实例、在线、one-pass 流式**的：模板库常驻内存，按时间递增顺序增量喂入。
因此服务端做了两层保护（误用即拒绝）：

1. **并行喂入保护**：同一时刻只允许一个 `/analyze` 在执行。并发调用会返回 `409 ConcurrentFeedError`。
2. **乱序喂入保护**：本次 `start_ms` 不得早于已处理到的时间上界。历史区间重喂会返回 `409 OutOfOrderFeedError`。

> 重新训练：先 `POST /reset` 清空，或分析时传 `allow_out_of_order: true`（仅调试用）。

### 错误码

| HTTP 状态 | 含义 |
|---|---|
| `200` | 成功 |
| `409` | 并行/乱序喂入被拒绝（误用保护），`detail` 含说明 |
| `422` | 请求体校验失败（字段类型/范围错误） |
| `500` | 服务内部错误 |

---

## 3. 接口列表

对外**核心业务接口只有 1 个**：`POST /analyze`，入参决定分析哪个服务单元 + 时间窗，
服务端据此调用日志查询接口拿到日志 → 在线流式解析 → 返回模板结果（闭环）。

| 方法 | 路径 | 类别 | 说明 |
|---|---|---|---|
| POST | `/analyze` | **核心（对外）** | 按服务单元+时间窗分析，直接返回模板结果 |
| GET | `/health` | 辅助/内部 | 健康检查 |
| GET | `/services` | 辅助/内部 | 列出可查询的服务单元 |
| GET | `/templates` | 辅助/内部 | 查看已发现模板与计数 |
| GET | `/summary` | 辅助/内部 | 全局概览 |
| POST | `/reset` | 辅助/内部 | 清空解析器，重新冷启动 |
| POST | `/save` | 辅助/内部 | 持久化模板库到 JSON |
| POST | `/load` | 辅助/内部 | 从 JSON 恢复模板库 |

> 辅助/内部接口用于排查与运维，不作为对外业务契约。真实接入时只需对接 `POST /analyze`。

---

## 4. 接口详情

### 4.1 GET /health

健康检查。

**响应** `200`：
```json
{ "message": "ok" }
```

---

### 4.2 GET /services

列出数据源中包含的服务单元（按 `appName` 去重）。

**响应** `200` `ServicesResponse`：
```json
{ "services": ["gateway-svc", "order-svc", "pay-svc", "inventory-svc"] }
```

---

### 4.3 POST /analyze

核心对外接口：按 `query`（服务单元）+ `start/end`（时间窗）查询日志并增量解析，
**返回每个错误模式的完整分析结果**。**可重复调用，模板库持续累积。**

**请求体** `AnalyzeRequest`：

| 字段 | 类型 | 必填 | 默认 | 说明 |
|---|---|---|---|---|
| `start` | int | 是 | `1787278400000` | 起始时间(ms, 含) |
| `end` | int | 是 | `1787282000000` | 截止时间(ms, 含) |
| `query` | string | 否 | 全部 | 查询条件：服务单元列表，逗号分隔（如 `order-unit-1,pay-unit-1`）；省略则查全部 |
| `batch_size` | int | 否 | `50` | 单次接口返回条数（可设 `>=10000`） |
| `allow_out_of_order` | bool | 否 | `false` | 允许乱序喂入（仅重新训练时用） |

**请求示例**：
```json
{
  "start": 1787278400000,
  "end": 1787282000000,
  "query": "order-unit-1,pay-unit-1",
  "batch_size": 500,
  "allow_out_of_order": false
}
```

**响应** `200` `AnalyzeResponse`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `services` | list[str] | 实际分析的服务单元 |
| `newly_processed` | int | 本批新增处理条数 |
| `total_processed` | int | 累计处理条数 |
| `window_total` | int | 本次时间窗内处理的日志总量 |
| `message_types` | int | 发现的错误模式（模板）数 |
| `patterns` | list[`PatternRecord`] | 错误模式明细（按数量降序） |

`PatternRecord` 单条模式结构：

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string | 日志模式（模板，可变参数标为 `*`） |
| `count` | int | 模式日志量（命中条数） |
| `ratio` | float | 模式日志量占总量的比例(%) |
| `first_seen_ms` | int | 首次出现时间(ms) |
| `last_seen_ms` | int | 最后出现时间(ms) |
| `trend` | list[`TrendPoint`] | 数据趋势：按时间均分 10 桶的计数序列 |
| `change_type` | string | 数量变化类型：`持续存在` / `新增` |
| `error_type` | string | 错误类型：`网络异常` / `业务异常` / `平台问题` |
| `sample` | string | 样例日志（原始 message） |

`TrendPoint`：`{ "bucket_start_ms": int, "count": int }`

```json
{
  "services": ["order-unit-1"],
  "newly_processed": 500,
  "total_processed": 500,
  "window_total": 500,
  "message_types": 15,
  "patterns": [
    {
      "template": "io.lettuce.core.RedisConnectionException Unable to connect to *",
      "count": 56,
      "ratio": 11.2,
      "first_seen_ms": 1787278423000,
      "last_seen_ms": 1787281989000,
      "trend": [ {"bucket_start_ms": 1787278400000, "count": 4}, {"bucket_start_ms": 1787278460000, "count": 6} ],
      "change_type": "新增",
      "error_type": "网络异常",
      "sample": "io.lettuce.core.RedisConnectionException: Unable to connect to redis-1.svc.local:6379"
    }
  ]
}
```

> 错误类型分类规则（基于模板内容关键字）：超时/连接失败/通信异常 → `网络异常`；
> HikariPool/lettuce/事务超时/懒加载 → `平台问题`；死锁/唯一键/外键/字段超长/SQL语法 → `业务异常`。
> `change_type` 由「分析前模板库是否已存在该模式」判定：已存在=持续存在，新出现=新增。

> 核心要点：`/analyze` 一个接口即完成「调日志查询接口 → 解析 → 返回完整模式结果」全链路，无需再调其他接口。

**错误**：
- `409`：并行或乱序喂入被拒绝，`detail` 含原因与处置建议。
- `422`：字段校验失败。

---

### 4.4 GET /templates

查看已发现的消息模板与计数。可按服务单元维度过滤（配合 `by_dim` 使用）。

**查询参数**：

| 参数 | 类型 | 默认 | 说明 |
|---|---|---|---|
| `service` | string | `null` | 服务单元维度过滤（对应 `by_dim=serviceUinitId` 时的分组 key，即 serviceUinitId 值） |
| `limit` | int | `20` | 返回条数，`[1, 200]` |

**响应** `200` `TemplatesResponse`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `service` | string | 过滤维度（无则 `null`） |
| `count` | int | 返回模板数 |
| `templates` | list[`TemplateItem`] | 模板列表 |

`TemplateItem`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `template` | string | 解析出的模板，可变参数标为 `*` |
| `count` | int | 命中次数 |

**响应示例**：
```json
{
  "service": "order-svc",
  "count": 18,
  "templates": [
    { "template": "HikariPool-1 - Connection is not available request timed out after *", "count": 112 },
    { "template": "Deadlock found when trying to get lock; try restarting transaction transaction id *", "count": 111 }
  ]
}
```

---

### 4.5 GET /summary

全局分析概览：总量、类型数、预过滤命中分布、Top 模板。

**响应** `200` `SummaryResponse`：

| 字段 | 类型 | 说明 |
|---|---|---|
| `total_processed` | int | 累计处理条数 |
| `message_types` | int | 模板（类型）数 |
| `prefilter_stats` | object | 三级预过滤命中分布：`prefix_tree` / `simple_loop` / `naive_lcs` / `new_type` |
| `top_templates` | list[`TemplateItem`] | Top-10 模板（按计数降序） |

**响应示例**：
```json
{
  "total_processed": 2000,
  "message_types": 18,
  "prefilter_stats": { "prefix_tree": 1962, "simple_loop": 0, "naive_lcs": 20, "new_type": 18 },
  "top_templates": [
    { "template": "HikariPool-1 - Connection is not available request timed out after *", "count": 112 }
  ]
}
```

---

### 4.6 POST /reset

清空解析器，重新冷启动（等价于丢弃模板库）。

**响应** `200`：
```json
{ "message": "analyzer reset" }
```

---

### 4.7 POST /save

持久化模板库到 JSON 文件。

**查询参数**：`path`（string，默认 `spell_model.json`）

**响应** `200`：
```json
{ "message": "saved to spell_model.json" }
```

---

### 4.8 POST /load

从 JSON 文件恢复模板库。

**查询参数**：`path`（string，默认 `spell_model.json`）

**响应** `200`：
```json
{ "message": "loaded from spell_model.json" }
```

---

## 5. 测试数据集

项目提供一份可直接用于测试的日志样例（`demo/gen_test_logs.py` 生成，`demo/test_logs.jsonl`）：

- 总量 2000 条，JSONL 格式
- 4 个服务单元（serviceUinitId）：`gateway-unit-1 / order-unit-1 / pay-unit-1 / inventory-unit-1`，归属 4 个应用（appName）
- 18 类 Java 层数据库操作报错（连接池/MySQL/Oracle/PostgreSQL/Redis/MongoDB/事务/MyBatis/Hibernate/外键/唯一键/锁等待/死锁/字段超长/SQL 语法…）
- 时间窗：`1787278400000 ~ 1787282000000`（`2026-08-21 10:13:20 ~ 11:13:20`）

以 file 数据源启动后即可测试（推荐 `main.py --serve` 统一入口）：
```bash
python main.py --serve --file demo/test_logs.jsonl --port 8000
# 或：LOG_SOURCE=file LOG_FILE=demo/test_logs.jsonl uvicorn api:app --port 8000
```

重新生成/调规模：
```bash
./venv/bin/python demo/gen_test_logs.py --total 5000 --out /tmp/test.jsonl
```

---

## 6. 典型调用示例（curl）

```bash
# 健康检查
curl -s http://127.0.0.1:8000/health

# 服务单元列表
curl -s http://127.0.0.1:8000/services

# 全量分析（batch=500 触发多页）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' \
  -d '{"start":1787278400000,"end":1787282000000,"batch_size":500}'

# 指定服务单元分析（query 写查询条件）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' \
  -d '{"start":1787278400000,"end":1787282000000,"query":"order-unit-1,pay-unit-1","batch_size":500}'
curl -s "http://127.0.0.1:8000/templates?service=order-unit-1&limit=20"

# 全局概览
curl -s http://127.0.0.1:8000/summary | python3 -m json.tool

# 乱序保护测试（已分析过整段后，再喂更早子区间 -> 409）
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' \
  -d '{"start_ms":1787278400000,"end_ms":1787279000000,"batch_size":500}'

# 重新训练
curl -s -X POST http://127.0.0.1:8000/reset
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' \
  -d '{"start_ms":1787278400000,"end_ms":1787282000000,"batch_size":500}'
```
