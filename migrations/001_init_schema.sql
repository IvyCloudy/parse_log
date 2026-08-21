-- ============================================================================
-- Spell 日志分析服务 — MySQL 8 持久化 Schema (migration 001)
-- 编码：utf8mb4 / 排序：utf8mb4_0900_ai_ci
-- 适用：MySQL 8.0+（用到了 utf8mb4_0900_ai_ci、JSON、CTE、ON DUPLICATE KEY UPDATE）
-- ----------------------------------------------------------------------------
-- 设计依据：现有 JSON 持久化结构（Spell.to_dict + LogAnalyzer._by_dim 的 .dim）
--   - 全局模板库 / 维度模板库：spell_template (+ spell_template_param)
--   - 每次 analyze 的模式统计：analyze_run (+ analyze_pattern)
--   - 解析器元信息（tau/stats/total_processed）：spell_meta（按 dimension 分桶）
--   - 维度枚举：dim_bucket（全局 / 每个 appName 等）
-- ============================================================================

CREATE DATABASE IF NOT EXISTS `spell_log`
  DEFAULT CHARACTER SET utf8mb4
  DEFAULT COLLATE utf8mb4_0900_ai_ci;

USE `spell_log`;

-- ----------------------------------------------------------------------------
-- 1. spell_meta：解析器元信息 KV 表
--    对应 Spell.to_dict 的 tau / total_processed / stats。
--    dimension='__global__' 代表主 spell；其余为 by_dim 维度值。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `spell_meta` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `dimension`   VARCHAR(255) NOT NULL                COMMENT '维度值；全局主解析器固定为 __global__',
  `tau`         DOUBLE       NOT NULL DEFAULT 0.5     COMMENT 'LCS 匹配阈值',
  `total_processed` BIGINT   NOT NULL DEFAULT 0      COMMENT '累计处理日志条数',
  `stats`       JSON         NULL                    COMMENT '预过滤命中分布 {prefix_tree,simple_loop,naive_lcs,new_type}',
  `updated_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dimension` (`dimension`)
) ENGINE=InnoDB COMMENT='解析器元信息（每 dimension 一行）';

-- ----------------------------------------------------------------------------
-- 2. dim_bucket：维度枚举表
--    记录有哪些维度分桶（便于 list 查询与迁移时枚举），避免扫全表。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `dim_bucket` (
  `dimension`   VARCHAR(255) NOT NULL,
  `created_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`dimension`)
) ENGINE=InnoDB COMMENT='维度分桶枚举（含 __global__）';

-- ----------------------------------------------------------------------------
-- 3. spell_template：模板主表（核心）
--    对应每个 LCSObject。dimension 区分全局/维度；template_hash 为内容指纹用于幂等 upsert。
--    seq（token 序列）与 template（join 后文本）均保留，便于查询与重建。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `spell_template` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `dimension`   VARCHAR(255) NOT NULL                COMMENT '所属维度（同 spell_meta.dimension）',
  `template_hash` VARCHAR(64) NOT NULL               COMMENT '模板指纹（sha1 of template 文本），用于幂等 upsert',
  `template`    TEXT         NOT NULL                COMMENT '模板文本（token 以空格连接，* 为参数占位）',
  `seq`         JSON         NOT NULL                COMMENT '模板 token 序列（数组，参数位置为 "*"）',
  `count`       BIGINT       NOT NULL DEFAULT 0      COMMENT '命中日志条数（对应 LCSObject.count）',
  `line_ids`    JSON         NULL                    COMMENT '归属日志行 id 集合（截断前100，对应 LCSObject.line_ids）',
  `last_seen`   BIGINT       NULL                    COMMENT '最后出现时间戳(ms)（对应 LCSObject.last_seen）',
  `created_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  `updated_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3) ON UPDATE CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_dim_template` (`dimension`, `template_hash`),
  KEY `idx_dimension` (`dimension`),
  KEY `idx_count` (`count`)
) ENGINE=InnoDB COMMENT='日志模板（消息类型）主表';

-- ----------------------------------------------------------------------------
-- 4. spell_template_param：参数样本（一对多）
--    对应 LCSObject.params_sample（最近 5 次参数值，每条为 token 列表）。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `spell_template_param` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `template_id` BIGINT       NOT NULL,
  `sample_idx`  INT          NOT NULL                COMMENT '样本序号（0..n，FIFO）',
  `params`      JSON         NOT NULL                COMMENT '单次参数值（数组，与模板 * 位置对齐）',
  `created_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `idx_template` (`template_id`),
  CONSTRAINT `fk_param_template` FOREIGN KEY (`template_id`)
    REFERENCES `spell_template` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='模板参数样本（最近5次）';

-- ----------------------------------------------------------------------------
-- 5. analyze_run：每次 analyze 调用批次记录
--    对应 LogAnalyzer.analyze 返回值顶层字段。
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `analyze_run` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `services`    JSON         NULL                    COMMENT '本次分析的服务单元列表',
  `start_ms`    BIGINT       NULL,
  `end_ms`      BIGINT       NULL,
  `by_dim`      VARCHAR(255) NULL                    COMMENT '本次 group-by 维度字段名（若有）',
  `newly_processed` BIGINT   NOT NULL DEFAULT 0,
  `total_processed`  BIGINT  NOT NULL DEFAULT 0,
  `window_total`     BIGINT  NOT NULL DEFAULT 0,
  `message_types`    INT     NOT NULL DEFAULT 0,
  `created_at`  DATETIME(3)  NOT NULL DEFAULT CURRENT_TIMESTAMP(3),
  PRIMARY KEY (`id`),
  KEY `idx_created` (`created_at`)
) ENGINE=InnoDB COMMENT='analyze 调用批次';

-- ----------------------------------------------------------------------------
-- 6. analyze_pattern：单次 analyze 的模式明细（对应 AnalyzeResponse.patterns）
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS `analyze_pattern` (
  `id`          BIGINT       NOT NULL AUTO_INCREMENT,
  `run_id`      BIGINT       NOT NULL,
  `rank`        INT          NOT NULL                COMMENT '按数量降序排名',
  `template`    TEXT         NOT NULL,
  `template_hash` VARCHAR(64) NOT NULL,
  `count`       BIGINT       NOT NULL DEFAULT 0,
  `ratio`       DOUBLE       NOT NULL DEFAULT 0       COMMENT '占总量比例(%)',
  `first_seen_ms` BIGINT     NULL,
  `last_seen_ms`  BIGINT     NULL,
  `trend`       JSON         NULL                    COMMENT '10桶趋势 [{bucket_start_ms,count}]',
  `change_type` VARCHAR(32)  NULL                    COMMENT '持续存在 / 新增',
  `error_type`  VARCHAR(32)  NULL                    COMMENT '网络异常/平台问题/业务异常/未知',
  `sample`      TEXT         NULL                    COMMENT '样例原始日志',
  PRIMARY KEY (`id`),
  KEY `idx_run` (`run_id`),
  KEY `idx_run_rank` (`run_id`, `rank`),
  CONSTRAINT `fk_pattern_run` FOREIGN KEY (`run_id`)
    REFERENCES `analyze_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB COMMENT='analyze 模式明细';

-- ----------------------------------------------------------------------------
-- 可复用 upsert 片段（供应用层 store.py 调用，便于增量更新）：
--
-- INSERT INTO spell_template
--   (dimension, template_hash, template, seq, count, line_ids, last_seen)
-- VALUES (%s, %s, %s, %s, %s, %s, %s)
-- ON DUPLICATE KEY UPDATE
--   count = count + VALUES(count),
--   line_ids = VALUES(line_ids),
--   last_seen = GREATEST(COALESCE(last_seen, 0), VALUES(last_seen)),
--   updated_at = CURRENT_TIMESTAMP(3);
-- ----------------------------------------------------------------------------
