-- =============================================================================
-- REV54: M1/S2 autonomy executor lease, heartbeat and graph version columns
-- =============================================================================
-- Extends t_ai_autonomous_run with the worker lease fields used for
-- idempotent Run claiming and startup recovery scans, plus the stored
-- graph_version that pins each Run to a compatible graph on upgrade.
-- The feature stays disabled until OGS_AI_AUTONOMY_ENABLED is set; these
-- columns are inert meanwhile. Safe to re-run: every ALTER is guarded.

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'lease_owner'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `lease_owner` VARCHAR(64) DEFAULT NULL',
    'SELECT "lease_owner exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'lease_expires_at'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `lease_expires_at` DATETIME DEFAULT NULL',
    'SELECT "lease_expires_at exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'heartbeat_at'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `heartbeat_at` DATETIME DEFAULT NULL',
    'SELECT "heartbeat_at exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'graph_version'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `graph_version` VARCHAR(32) NOT NULL DEFAULT ''v1''',
    'SELECT "graph_version exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SET @idx_exists := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND INDEX_NAME = 'idx_ai_auto_run_lease_expires'
);
SET @sql := IF(@idx_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD KEY `idx_ai_auto_run_lease_expires` (`lease_expires_at`)',
    'SELECT "idx_ai_auto_run_lease_expires exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
