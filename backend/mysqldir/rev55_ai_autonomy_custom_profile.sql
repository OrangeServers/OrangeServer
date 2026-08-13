-- =============================================================================
-- REV55: M1/S3 autonomy custom permission profile column
-- =============================================================================
-- Extends t_ai_autonomous_run with the optional custom permission profile
-- (server-fixed action categories combined with the Run's single bound
-- host). The column stays NULL for ask/ai_review/auto Runs. The feature
-- stays disabled until OGS_AI_AUTONOMY_ENABLED is set; this column is
-- inert meanwhile. Safe to re-run: the ALTER is guarded.

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_run'
      AND COLUMN_NAME = 'custom_profile_json'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `custom_profile_json` TEXT DEFAULT NULL',
    'SELECT "custom_profile_json exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
