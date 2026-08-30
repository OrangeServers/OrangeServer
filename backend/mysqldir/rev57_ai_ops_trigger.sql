-- =============================================================================
-- REV57: M2/S1 external trigger identity for Autonomy Runs
-- =============================================================================
-- Existing rows remain manual. External idempotency references are opaque
-- hashes; nullable manual/chat references do not collide under MySQL UNIQUE.

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'trigger_type'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `trigger_type` varchar(16) NOT NULL DEFAULT ''manual'' AFTER `mode`',
    'SELECT "trigger_type exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'trigger_ref'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `trigger_ref` varchar(64) DEFAULT NULL AFTER `trigger_type`',
    'SELECT "trigger_ref exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'trigger_summary'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `trigger_summary` varchar(512) NOT NULL DEFAULT '''' AFTER `trigger_ref`',
    'SELECT "trigger_summary exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @index_exists := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND INDEX_NAME = 'uq_ai_auto_run_trigger'
);
SET @sql := IF(@index_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD UNIQUE KEY `uq_ai_auto_run_trigger` (`trigger_type`, `trigger_ref`)',
    'SELECT "uq_ai_auto_run_trigger exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
