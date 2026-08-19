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
      AND COLUMN_NAME = 'lease_token'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `lease_token` VARCHAR(64) DEFAULT NULL AFTER `lease_owner`',
    'SELECT "lease_token exists" AS info');
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

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'active_host_id'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `active_host_id` INT GENERATED ALWAYS AS (CASE WHEN `status` IN (_utf8mb4''completed'', _utf8mb4''failed'', _utf8mb4''cancelled'', _utf8mb4''expired'') THEN NULL ELSE `host_id` END) STORED',
    'SELECT "active_host_id exists" AS info');
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

-- Adding the unique key intentionally fails closed if an experimental
-- database already contains more than one active Run for the same host.
-- Operators must inspect and reconcile those Runs; this migration never
-- chooses a winner or deletes durable state automatically.
SELECT `host_id`, COUNT(*) AS `active_run_count`
FROM `t_ai_autonomous_run`
WHERE `active_host_id` IS NOT NULL
GROUP BY `host_id`
HAVING COUNT(*) > 1;

SET @idx_exists := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND INDEX_NAME = 'uq_ai_auto_run_active_host'
);
SET @sql := IF(@idx_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD UNIQUE KEY `uq_ai_auto_run_active_host` (`active_host_id`)',
    'SELECT "uq_ai_auto_run_active_host exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
