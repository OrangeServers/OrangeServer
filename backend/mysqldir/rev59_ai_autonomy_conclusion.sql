-- M2: persist the bounded, operator-facing autonomy conclusion contract.

SET @has_conclusion_json = (
  SELECT COUNT(*) FROM information_schema.COLUMNS
  WHERE TABLE_SCHEMA = DATABASE()
    AND TABLE_NAME = 't_ai_autonomous_run'
    AND COLUMN_NAME = 'conclusion_json'
);
SET @add_conclusion_json = IF(
  @has_conclusion_json = 0,
  'ALTER TABLE `t_ai_autonomous_run` ADD COLUMN `conclusion_json` TEXT DEFAULT NULL AFTER `outcome`',
  'SELECT "conclusion_json exists" AS info'
);
PREPARE stmt FROM @add_conclusion_json;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
