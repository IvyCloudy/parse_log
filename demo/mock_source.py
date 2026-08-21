"""Mock 日志数据源（仅用于本地开发 / 演示，不属于生产代码）。

独立成模块，避免与正式数据源（HTTP / 文件，见 data_sources.py）混在一起。

场景：不同类型数据库操作的业务，错误日志对应 Java 层面的常见数据库报错，例如：
  - 连接池耗尽 (HikariCP / Druid)
  - JDBC 连接/通信失败 (Communications link failure / Broken pipe)
  - 事务异常 (Transaction timed out / Deadlock / Lock wait timeout)
  - SQL 语法 / 约束错误 (MySQL / Oracle / PostgreSQL 方言)
  - MyBatis / Hibernate / JPA 映射异常
  - Redis / MongoDB 客户端报错
  - 主键冲突 / 唯一约束 / 外键约束 / 字段超长

一条日志(logData)同时包含：
  - appName        应用名称（如 gateway-app）
  - serviceUinitId 服务单元 ID（如 gateway-unit-1）—— 这才是分页/分组用的
                    「服务单元」维度，一个应用可包含多个服务单元(1:N)

入参 = 服务单元(serviceUinitId) + 起始/截止时间，单次可返回万条以上，
返回结构与 log-format.json 一致（已补充 serviceUinitId 字段）。
"""

from __future__ import annotations

import datetime
import os
import sys
from typing import List, Optional

# demo/ 是开发/演示目录，运行根目录可能在项目根，也可能在别处。
# 把项目根目录加入 sys.path，保证能 import 到正式模块 data_sources。
_THIS_DIR = os.path.dirname(os.path.abspath(__file__))
_PROJECT_ROOT = os.path.dirname(_THIS_DIR)
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

from data_sources import LogPage, LogDataSource, log_date_to_ms


# 服务单元 ID -> 所属应用名称（1:N：一个应用可含多个服务单元）。
# serviceUinitId 是分页/分组的「服务单元」维度；appName 仅表示应用名称。
_UNIT_TO_APP = {
    "gateway-unit-1": "gateway-app",
    "order-unit-1": "order-app",
    "pay-unit-1": "pay-app",
    "inventory-unit-1": "inventory-app",
}
_SERVICES = list(_UNIT_TO_APP.keys())  # 服务单元 ID 列表


def _log_date(ts_ms: int) -> str:
    dt = datetime.datetime.fromtimestamp(ts_ms / 1000)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{ts_ms % 1000:03d}000+0800"


def _make_log(message: str, unit: str, level: str, ts_ms: int,
              logger: str, method: str) -> dict:
    return {
        "logType": "APP",
        "level": level,
        "message": message,
        "logDate": _log_date(ts_ms),
        "traceId": f"trace-{ts_ms % 100000:05d}",
        "method": method,
        "logger": logger,
        "appName": _UNIT_TO_APP.get(unit, unit),  # 应用名称
        "serviceUinitId": unit,                    # 服务单元 ID（分页/分组维度）
    }


# ---------------------------------------------------------------------------
# 不同数据库操作业务的模板。每条模板是一个可调用的生成器，
# 接收 (svc, ts_ms, i) 返回一条 dict 日志。
# 这样 Spell 解析时既能合并出稳定模板，又能保留可变参数（id/表名/时间等）。
# ---------------------------------------------------------------------------

# 业务域常见的 logger / method（贴近真实 Java 栈）
_JDBC_LOGGER = "com.zaxxer.hikari.pool.HikariProxyConnection"
_JPA_LOGGER = "org.hibernate.engine.jdbc.spi.SqlExceptionHelper"
_MYBATIS_LOGGER = "org.mybatis.spring.MyBatisSystemException"
_REDIS_LOGGER = "org.springframework.data.redis.connection.lettuce.LettuceConnection"


def _tpl_hikari_timeout(svc, ts_ms, i):
    wait = 30000 + (i % 5) * 1000
    return _make_log(
        f"HikariPool-1 - Connection is not available, request timed out after {wait}ms.",
        svc, "ERROR", ts_ms, _JDBC_LOGGER,
        "com.zaxxer.hikari.pool.HikariPool.getConnection(HikariPool.java:696)")


def _tpl_hikari_exhausted(svc, ts_ms, i):
    active = 20 + i % 5
    return _make_log(
        f"HikariPool-1 is exhausted, active/ idle connections = {active}/0, "
        f"waiting threads = {active}",
        svc, "ERROR", ts_ms, _JDBC_LOGGER,
        "com.zaxxer.hikari.pool.HikariPool.createTimeoutException(HikariPool.java:696)")


def _tpl_comm_link_failure(svc, ts_ms, i):
    host = f"10.20.{i % 5}.{i % 250}"
    return _make_log(
        f"com.mysql.cj.jdbc.exceptions.CommunicationsException: Communications link "
        f"failure\nThe last packet sent to the server was {100 + i % 50} ms ago on "
        f"{host}:3306.",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.SQLError.createCommunicationsException(SQLError.java:174)")


def _tpl_broken_pipe(svc, ts_ms, i):
    return _make_log(
        f"com.mysql.cj.jdbc.exceptions.MySQLNonTransientConnectionException: "
        f"Broken pipe (Write failed) when writing to association mysql connection",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "sun.nio.ch.SocketDispatcher.write(SocketDispatcher.java:47)")


def _tpl_deadlock_mysql(svc, ts_ms, i):
    txn = 1000 + i % 900
    return _make_log(
        f"Deadlock found when trying to get lock; try restarting transaction, "
        f"transaction id {txn}",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException.<init>(...)")


def _tpl_lock_wait(svc, ts_ms, i):
    sec = 50 + i % 10
    tbl = ["orders", "payments", "inventory", "accounts"][i % 4]
    return _make_log(
        f"Lock wait timeout exceeded; try restarting transaction, table `{tbl}` "
        f"waited {sec}s",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLTransactionRollbackException.<init>(...)")


def _tpl_unique_violation(svc, ts_ms, i):
    tbl = ["t_order", "t_user", "t_pay", "t_stock"][i % 4]
    key = 100000 + i
    return _make_log(
        f"Duplicate entry '{key}' for key 'PRIMARY' on table {tbl}",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLIntegrityConstraintViolationException.<init>(...)")


def _tpl_oracle_ora(svc, ts_ms, i):
    codes = ["ORA-12170", "ORA-12514", "ORA-00060", "ORA-01555"]
    code = codes[i % len(codes)]
    return _make_log(
        f"java.sql.SQLRecoverableException: ORA-{code[4:]}: TNS: operation timed out "
        f"connecting to oracle host 192.168.{i % 5}.{i % 200}",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "oracle.jdbc.driver.T4CConnection.logon(T4CConnection.java:743)")


def _tpl_pg_exception(svc, ts_ms, i):
    codes = ["08006", "40P01", "23505", "53400"]
    code = codes[i % len(codes)]
    return _make_log(
        f"org.postgresql.util.PSQLException: ERROR: {code}: deadlock detected on "
        f"relation orders pid={5000 + i}",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "org.postgresql.core.v3.QueryExecutorImpl.receiveErrorResponse(...)")


def _tpl_tx_timeout(svc, ts_ms, i):
    sec = 30 + i % 20
    return _make_log(
        f"Transaction timed out: deadline exceeded after {sec}s; nested exception is "
        f"org.springframework.transaction.TransactionTimedOutException",
        svc, "ERROR", ts_ms, "org.springframework.transaction.support.ResourceHolderSupport",
        "org.springframework.transaction.support.ResourceHolderSupport.checkTransactionTimeout(...)")


def _tpl_mybatis_result(svc, ts_ms, i):
    col = ["user_id", "order_no", "amount", "sku_code"][i % 4]
    return _make_log(
        f"### Error querying database.  Cause: "
        f"org.apache.ibatis.reflection.ReflectionException: Could not set property "
        f"'{col}' of 'class com.demo.entity.Order'",
        svc, "ERROR", ts_ms, _MYBATIS_LOGGER,
        "org.mybatis.spring.MyBatisExceptionTranslator.translateExceptionIfPossible(...)")


def _tpl_hibernate_lazy(svc, ts_ms, i):
    ent = ["Order", "User", "Payment", "Stock"][i % 4]
    return _make_log(
        f"org.hibernate.LazyInitializationException: could not initialize proxy - "
        f"no Session for {ent}#{1000 + i}",
        svc, "ERROR", ts_ms, "org.hibernate.LazyInitializationException",
        "org.hibernate.proxy.AbstractLazyInitializer.initialize(...)")


def _tpl_redis_timeout(svc, ts_ms, i):
    cmd = ["GET", "SET", "HGET", "EXPIRE"][i % 4]
    key = f"cache:order:{1000 + i}"
    return _make_log(
        f"RedisCommandTimeoutException: Command {cmd} timed out after 1s for key {key}",
        svc, "ERROR", ts_ms, _REDIS_LOGGER,
        "io.lettuce.core.RedisCommandTimeoutException.<init>(...)")


def _tpl_redis_conn_refused(svc, ts_ms, i):
    host = f"redis-{i % 3}.svc.local"
    return _make_log(
        f"io.lettuce.core.RedisConnectionException: Unable to connect to "
        f"{host}:6379; Connection refused",
        svc, "ERROR", ts_ms, _REDIS_LOGGER,
        "io.lettuce.core.RedisConnectionException.create(...)")


def _tpl_mongo_timeout(svc, ts_ms, i):
    coll = ["orders", "users", "payments"][i % 3]
    return _make_log(
        f"MongoSocketReadTimeoutException: Timed out after 30000 ms while waiting for "
        f"a socket to read from collection {coll}",
        svc, "ERROR", ts_ms, "org.springframework.data.mongodb.core.MongoTemplate",
        "com.mongodb.connection.SocketStream.read(SocketStream.java:91)")


def _tpl_sql_syntax(svc, ts_ms, i):
    tbl = ["t_order", "t_user", "t_pay"][i % 3]
    return _make_log(
        f"You have an error in your SQL syntax; check the manual that corresponds to "
        f"your MySQL server version for the right syntax to use near 'SELECT * FROM "
        f"{tbl} WHERE id = {1000 + i}'",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLSyntaxErrorException.<init>(...)")


def _tpl_data_too_long(svc, ts_ms, i):
    col = ["phone", "address", "remark", "card_no"][i % 4]
    return _make_log(
        f"Data too long for column '{col}' at row 1, max length exceeded inserting "
        f"record id {2000 + i}",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLDataException.<init>(...)")


def _tpl_foreign_key(svc, ts_ms, i):
    tbl = ["t_order_item", "t_pay_record", "t_stock_log"][i % 3]
    return _make_log(
        f"Cannot add or update a child row: a foreign key constraint fails "
        f"(`demo`.`{tbl}`, CONSTRAINT `fk_parent`)",
        svc, "ERROR", ts_ms, _JPA_LOGGER,
        "com.mysql.cj.jdbc.exceptions.MySQLIntegrityConstraintViolationException.<init>(...)")


# 全部模板（覆盖不同数据库与操作场景），按权重轮询
_TEMPLATES = [
    _tpl_hikari_timeout,        # 连接池超时
    _tpl_hikari_exhausted,      # 连接池耗尽
    _tpl_comm_link_failure,     # MySQL 通信失败
    _tpl_broken_pipe,           # 连接断开
    _tpl_deadlock_mysql,        # MySQL 死锁
    _tpl_lock_wait,             # 锁等待超时
    _tpl_unique_violation,      # 主键/唯一冲突
    _tpl_oracle_ora,            # Oracle ORA 报错
    _tpl_pg_exception,          # PostgreSQL 异常
    _tpl_tx_timeout,            # Spring 事务超时
    _tpl_mybatis_result,        # MyBatis 映射异常
    _tpl_hibernate_lazy,        # Hibernate 懒加载
    _tpl_redis_timeout,         # Redis 命令超时
    _tpl_redis_conn_refused,    # Redis 连接拒绝
    _tpl_mongo_timeout,         # MongoDB 读取超时
    _tpl_sql_syntax,            # SQL 语法错误
    _tpl_data_too_long,         # 字段超长
    _tpl_foreign_key,           # 外键约束
]


class MockSource(LogDataSource):
    """Mock 数据源：生成不同类型数据库操作的 Java 层报错日志。

    入参 = 服务单元(serviceUinitId) + 起始/截止时间，单次可返回万条以上。
    每条 logData 同时带 appName(应用名) 与 serviceUinitId(服务单元 ID)。
    """

    SERVICES = list(_SERVICES)  # 服务单元 ID 列表

    def __init__(self, batch_size: int = 50, total: int = 12000,
                 span_ms: int = 3_600_000, base_ts: int = 1787278400000):
        self.batch_size = batch_size
        self.total = total
        self.base_ts = base_ts
        self.span_ms = span_ms
        self._all = self._build(total)

    def _build(self, total: int) -> List[dict]:
        out: List[dict] = []
        n_tpl = len(_TEMPLATES)
        for i in range(total):
            ts_ms = self.base_ts + int(self.span_ms * i / max(1, total - 1))
            unit = _SERVICES[i % len(_SERVICES)]
            # 用 i 的低几位在模板间轮询，保证每类报错都有足够样本供 Spell 合并
            tpl = _TEMPLATES[i % n_tpl]
            out.append(tpl(unit, ts_ms, i))
        return out

    def list_services(self) -> List[str]:
        # 返回服务单元 ID（serviceUinitId），而非 appName
        return list(self.SERVICES)

    def query_page(self, service: str, start_ms: int, end_ms: int,
                   limit: int = 50) -> LogPage:
        # service 参数 == 服务单元 ID (serviceUinitId)
        matched = [
            log for log in self._all
            if log["serviceUinitId"] == service
            and start_ms <= log_date_to_ms(log["logDate"]) <= end_ms
        ]
        chunk = matched[:limit]
        next_start = (log_date_to_ms(chunk[-1]["logDate"]) + 1) if len(matched) > limit else None
        return LogPage(items=chunk, next_start=next_start)
