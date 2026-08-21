# 基于 Spell 的批量错误日志在线解析服务

将 Spell 论文（基于 LCS 的流式日志解析）思路落地为 FastAPI 服务，对"按服务单元 + 起止时间分批查询"的错误日志做在线流式解析，自动提取错误模板（参数标 `*`）并统计频次。

适用于不同数据库操作业务的 Java 应用错误日志（连接池 / MySQL / Oracle / PostgreSQL / Redis / MongoDB / 事务 / MyBatis / Hibernate 等常见报错）。

---

## 文档索引

| 文档 | 说明 |
|---|---|
| [docs/requirements.md](./docs/requirements.md) | **项目需求文档**：背景、功能需求(FR-1~6)、数据模型、约束、验收标准 |
| [docs/api_docs.md](./docs/api_docs.md) | **接口说明文档**：启动配置、8 个端点、请求/响应字段、curl 示例 |
| [docs/spell_翻译.md](./docs/spell_翻译.md) | 论文全文翻译 |
| [docs/spell_大纲与提炼.md](./docs/spell_大纲与提炼.md) | 论文大纲与关键实现提炼 |
| [docs/spell_翻译与提炼.md](./docs/spell_翻译与提炼.md) | 论文翻译与提炼合并版 |

---

## 快速上手

### 依赖
```bash
pip install fastapi uvicorn pydantic requests
# 或使用项目 venv：./venv/bin/python -m pip install -r requirements.txt
```

### 配置（config.yaml + 环境变量，环境变量优先）
项目根 `config.yaml` 可配置数据源与解析参数；也可用环境变量覆盖（兼容 `LOG_SOURCE` 等历史命名）。详见 [docs/api_docs.md](./docs/api_docs.md) §1。

### 启动（统一入口 `main.py --serve`，三种数据源）

推荐用 `main.py --serve` 启动 FastAPI 服务（内部即 uvicorn 运行 `api:app`，数据源参数自动透传为环境变量）：

```bash
# 演示：本地生成万条数据库报错 mock 数据
python main.py --serve --host 0.0.0.0 --port 8000

# 本地文件：demo/test_logs.jsonl（默认 2000 条，4 服务/4 应用/18 类报错）
python main.py --serve --file demo/test_logs.jsonl --port 8000
# 或写入 config.yaml: log_source: file / data_source.file: demo/test_logs.jsonl

# 生产：对接真实日志查询接口
python main.py --serve --real --url https://your-api/errors --port 8000
```

等价地，也可直接用 uvicorn 启动（需自行配置环境变量）：
```bash
LOG_SOURCE=file LOG_FILE=demo/test_logs.jsonl uvicorn api:app --port 8000
```

> `--reload` 可在开发时开启热重载；更多启动参数见 `python main.py --help`。

### 生成测试数据
```bash
./venv/bin/python demo/gen_test_logs.py --total 5000 --out /tmp/test.jsonl
```

### 典型调用
```bash
curl -s http://127.0.0.1:8000/health
# 核心接口：入参 start/end/query，出参含每个错误模式的首次/最后出现、占比、趋势、
# 变化类型(新增/持续存在)、错误类型(网络异常/业务异常/平台问题)、样例日志
curl -s -X POST http://127.0.0.1:8000/analyze -H 'Content-Type: application/json' \
  -d '{"start":1787278400000,"end":1787282000000,"query":"order-unit-1","batch_size":500}'
curl -s "http://127.0.0.1:8000/templates?service=order-unit-1&limit=20"
curl -s http://127.0.0.1:8000/summary | python3 -m json.tool
```

在线文档：`http://127.0.0.1:8000/docs`（Swagger）

---

## 目录结构与模块职责

```
parse_log/
├── spell_parser.py      # 核心算法：Spell / LCSObject / PrefixTree（零依赖）
├── data_sources.py      # 数据源抽象：LogDataSource 基类 / HttpSource / FileSource
├── log_client.py        # 真实日志查询 HTTP 客户端（独立文件，封装接口复杂性）
├── config.py            # 统一配置加载（config.yaml + 环境变量，环境优先）
├── config.yaml          # 配置文件示例（数据源 / 解析参数）
├── demo/
│   ├── mock_source.py   # 开发/演示用 Mock 数据源（不同数据库 Java 报错，万条级）
│   └── gen_test_logs.py # 生成测试日志 JSONL
├── analyzer.py          # 服务层：LogAnalyzer 驱动流式分析 + 并行/乱序保护
├── schemas.py           # Pydantic 请求/响应模型
├── api.py               # FastAPI 路由（对外核心接口 POST /analyze）
├── main.py              # CLI 入口，含 --serve 启动 FastAPI 服务
├── log-format.json      # 日志结构示例（含 serviceUinitId）
├── docs/                # 文档目录
│   ├── requirements.md  # 项目需求文档
│   ├── api_docs.md      # 接口说明文档
│   ├── spell_*.md / spell.pdf  # 论文翻译与提炼
└── README.md            # 本文档（总索引）
```

---

## 关键概念

- **服务单元 / 应用名称**：每条日志含 `serviceUinitId`（服务单元 ID，分页与分组维度）和 `appName`（应用名称），1 个应用可含多个服务单元。
- **流式语义约束**：解析器单实例、常驻内存、按时间递增 one-pass 喂入。并发喂入或时间倒退会被拒绝（HTTP 409）。
- **模板**：可变参数统一标为 `*`，如 `HikariPool-1 - Connection is not available request timed out after *`。

详见 [docs/requirements.md](./docs/requirements.md) 与 [docs/api_docs.md](./docs/api_docs.md)。
