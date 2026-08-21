# 项目需求文档：基于 Spell 的批量错误日志在线解析服务

> 版本：v1.0
> 状态：已实现并验证（2026-08）
> 关联文档：[`api_docs.md`](./api_docs.md)（接口说明）、`spell_翻译.md` / `spell_大纲与提炼.md`（论文翻译与提炼）

---

## 1. 背景与目标

### 1.1 背景
生产环境中存在大量"不同类型数据库操作业务"的 Java 应用，运行时会持续产生错误日志
（连接池耗尽、MySQL/Oracle/PostgreSQL 各类异常、Redis/MongoDB 超时、事务死锁、MyBatis/Hibernate
映射异常等）。这些日志通过"按服务单元 + 起止时间分批查询"的接口分页拉取，单批次可达万条以上。

直接阅读原始日志难以定位模式。需要一种**自动从批量错误日志中提取模板（参数化）**的能力，
帮助运维/开发快速掌握"当前系统在报哪几类错、每类频次多少"。

### 1.2 方法论
采用 Spell 论文（基于 LCS 的流式日志解析）的思路：
- 维护一个常驻内存的模板库（LCSMap），按 **one-pass 在线流式** 方式增量解析；
- 三级预过滤（前缀树 / 简单循环 / Jaccard+LCS 兜底）高效匹配已有模板，未命中则新建；
- 模板中可变参数用 `*` 占位，并支持回带提取具体参数值。

### 1.3 目标
将 Spell 解析能力封装为 **FastAPI 服务**，对外提供"分批查询日志 → 在线流式解析 → 模板聚合"
的 HTTP 接口，便于集成到现有日志平台或本地调试分析。

### 1.4 非目标（本期不做）
- 不实现实时推送/订阅（本期仅支持请求-响应式的批量拉取）。
- 不做根因分析 / 告警（仅做模板提取与统计）。
- 不做多实例分布式解析（受流式语义约束，见 §6）。

---

## 2. 干系人与用户故事

| 角色 | 诉求 |
|---|---|
| 运维/SRE | 给定服务单元和时间窗，快速得到"错误类型分布"与 Top 模板 |
| 开发者 | 按 `appName` / `level` / `logger` / `serviceUinitId` 等维度分组查看模板 |
| 平台集成方 | 通过 HTTP 接口把本服务接入现有日志平台 |

**用户故事**
1. 作为运维，我想按"服务单元 + 时间窗"提交分析，得到该区间的错误模板清单与计数。
2. 作为运维，我想重复提交相邻时间窗的分析，让模板库持续累积（在线流式）。
3. 作为开发者，我想按 `serviceUinitId` / `appName` 等维度分组，只看某服务的模板。
4. 作为调试者，我想用一组本地 mock 数据（不同数据库 Java 报错）直接跑通全流程，无需真实接口。
5. 作为平台方，我想通过 Swagger/OpenAPI 了解全部接口契约。

---

## 3. 功能需求

### FR-1 日志分批获取（数据源抽象）
- 系统须支持以统一接口获取日志：`list_services()` 返回服务单元列表；`query_page(serviceUinitId, start_ms, end_ms, limit)` 返回一页日志与翻页游标。
- 支持三种数据源，通过 `config.yaml` / 环境变量 `LOG_SOURCE` 切换：
  - `mock`：本地生成不同数据库 Java 报错的样例数据（万条级），**仅开发/演示用，不进入生产代码路径**。
  - `file`：读取本地 JSONL 日志文件（每条一行 logData）。
  - `http`：对接真实日志查询接口。**接口的复杂性（请求方式 GET/POST、鉴权 Bearer/签名、响应解析、分页策略 cursor/offset/time、字段映射）统一封装在独立文件 `log_client.py`**，默认契约遵循 `log-format.json`；真实接口复杂时通过 `LogClientConfig` 配置或继承 `LogClient` 重写钩子，分析流程不动。

### FR-2 在线流式解析
- 解析器（`spell_parser.Spell`）须为单实例、常驻内存、one-pass 流式：模板库持续累积，重复调用 `analyze` 不应重置。
- 须从日志 `message` 中提取模板，可变参数统一标为 `*`；支持回带提取参数值（`extract_params`）。

### FR-3 全局单一模板池
- `analyze` 使用全局单一 Spell 模板池（不按维度拆分）。`query` 入参用于指定要分析的服务单元集合，不额外引入维度分组参数。

### FR-4 分析查询接口（对外核心：`POST /analyze`）
- 入参：`start`（起始时间 ms）、`end`（截止时间 ms）、`query`（服务单元列表，逗号分隔）。
- 出参：每个错误模式的完整分析结果，含：
  - 日志模式（模板，参数标 `*`）；
  - 模式日志量（命中条数）与占总量的比例(%)；
  - 首次出现时间 / 最后出现时间（基于日志真实 logDate）；
  - 数据趋势（按时间均分 10 桶的计数序列）；
  - 数量变化类型：`持续存在` / `新增`（对比分析前模板库是否已存在该模式）；
  - 错误类型：`网络异常` / `业务异常` / `平台问题`（基于模板内容关键字分类）；
  - 样例日志（原始 message）。
- `GET /templates`：查看已发现模板与计数，可按 `serviceUinitId` 过滤（辅助/内部）。
- `GET /summary`：返回全局概览（总量、类型数、预过滤命中分布、Top 模板）。

### FR-5 生命周期管理
- `GET /health`：健康检查。
- `GET /services`：列出可查询的服务单元（serviceUinitId）。
- `POST /reset`：清空解析器，重新冷启动。
- `POST /save` / `POST /load`：模板库持久化与恢复（JSON 文件）。

### FR-6 误用保护（防流式语义破坏）
- **并行喂入保护**：同一时刻只允许一个 `/analyze` 执行；并发调用返回 `409 ConcurrentFeedError`。
- **乱序喂入保护**：本次 `start_ms` 不得早于已处理时间上界；历史区间重喂返回 `409 OutOfOrderFeedError`。
- 允许通过 `allow_out_of_order: true` 显式关闭乱序保护（仅重新训练调试用）。

---

## 4. 数据模型

### 4.1 单条日志（logData）
与 `log-format.json` 一致，关键字段：

| 字段 | 类型 | 说明 |
|---|---|---|
| `logType` | string | 日志类型，如 `APP` |
| `level` | string | 级别：`ERROR` / `WARN` / `INFO` |
| `message` | string | 日志正文（解析对象） |
| `logDate` | string | 时间戳，格式 `YYYY-MM-DDTHH:MM:SS.sssssss+0800` |
| `traceId` | string | 链路追踪 ID |
| `method` | string | 抛出位置（Java 栈） |
| `logger` | string | logger 名称 |
| `appName` | string | **应用名称**（如 `gateway-app`） |
| `serviceUinitId` | string | **服务单元 ID**（如 `gateway-unit-1`），分页/分组的"服务单元"维度 |

> 区分：`serviceUinitId` 是服务单元维度（接口 `query` 入参、分页均用它）；
> `appName` 仅表示应用名称，一个应用可含多个服务单元（1:N）。

### 4.2 数据源返回结构
- `LogPage { items: list[logData], next_start: int | null }`，`next_start` 为下一页游标（毫秒+1），`null` 表示无更多数据。

### 4.3 模板表示
- `TemplateItem { template: string, count: int }`，`template` 中可变参数标为 `*`。

---

## 5. 接口契约（摘要）

完整字段/示例见 [`api_docs.md`](./api_docs.md)。

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 健康检查 |
| GET | `/services` | 列出服务单元（serviceUinitId） |
| POST | `/analyze` | 提交分析（body：`start/end/query/batch_size/allow_out_of_order`） |
| GET | `/templates` | 查看模板（query：`service`/`limit`） |
| GET | `/summary` | 全局概览 |
| POST | `/reset` | 清空解析器 |
| POST | `/save` | 持久化模板库（query：`path`） |
| POST | `/load` | 恢复模板库（query：`path`） |

错误码：`200` 成功 / `409` 并行或乱序喂入被拒 / `422` 参数校验失败 / `500` 内部错误。

---

## 6. 关键约束与设计决策

### 6.1 流式语义约束（重要）
Spell 解析器是单实例在线流式模型，模板库常驻内存且按时间递增 one-pass 喂入。
因此：
- **不允许并行喂入**：多线程/多实例并发写会破坏模板合并正确性 → FR-6 并行保护。
- **不允许时间倒退**：历史区间重喂会污染模板 → FR-6 乱序保护。
- **多实例/多 worker 部署是反模式**：若需水平扩展，须把日志经队列串行路由到单解析实例，或用进程外锁（文件锁/Redis 锁）保证单写者。当前实现为进程内锁，仅保证单进程内安全。

### 6.2 数据源隔离
- Mock 数据源独立置于 `demo/` 目录，正式代码（`data_sources.py`）只保留 `HttpSource` / `FileSource`，避免演示代码污染生产路径。

### 6.3 性能预期
- 万条级单批日志可在秒级完成解析；前缀树预过滤命中率通常在 99% 以上，兜底 LCS 仅用于新类型/边界。
- `batch_size` 建议 ≥ 实际单批接口返回量，以充分利用一次 HTTP 往返。

---

## 7. 测试与验收

### 7.1 测试数据集
- `demo/gen_test_logs.py` 生成 `demo/test_logs.jsonl`（默认 2000 条）：
  覆盖 4 个服务单元、4 个应用、18 类 Java 层数据库操作报错、时间窗约 1 小时。
- 以 `LOG_SOURCE=file LOG_FILE=demo/test_logs.jsonl` 启动即可测试。

### 7.2 验收标准
1. `POST /analyze` 全量分析 2000 条 → `message_types = 18`、`newly_processed = 2000`。
2. `GET /templates?service=order-unit-1` 仅返回该服务单元模板。
3. 并发两次 `/analyze` → 其中一个返回 `409`（并行保护生效）。
5. 分析整段后再喂更早子区间 → 返回 `409`（乱序保护生效）；`POST /reset` 后可重新分析。
6. `GET /docs` 可浏览全部接口契约。

---

## 8. 目录结构与模块职责

```
parse_log/
├── spell_parser.py      # 核心算法：Spell/LCSObject/PrefixTree，零依赖
├── data_sources.py      # 数据源抽象：LogDataSource 基类 / HttpSource / FileSource
├── log_client.py        # 真实日志查询 HTTP 客户端（独立文件，封装接口复杂性）
├── config.py            # 统一配置加载（config.yaml + 环境变量，环境优先）
├── config.yaml          # 配置文件示例（数据源 / 解析参数）
├── demo/
│   ├── mock_source.py   # 开发/演示用 Mock 数据源（不同数据库 Java 报错，万条级）
│   └── gen_test_logs.py # 生成测试日志 JSONL
├── analyzer.py          # 服务层：LogAnalyzer 驱动流式分析 + 并行/乱序保护
├── schemas.py           # Pydantic 请求/响应模型
├── api.py               # FastAPI 路由
├── main.py              # CLI 入口
├── log-format.json      # 日志结构示例（含 serviceUinitId）
├── api_docs.md          # 接口说明文档
├── requirements.md      # 本文档
└── spell_*.md / spell.pdf  # 论文翻译与提炼
```

---

## 9. 后续演进（候选）
- 真实接口鉴权增强、服务发现 endpoint。
- 模板库版本化 / 时间衰减（旧模板降权）。
- 多实例安全的外部锁方案（Redis 锁 / 队列串行化）。
- 与告警、根因推荐联动。
