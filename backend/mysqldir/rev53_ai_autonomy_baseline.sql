-- =============================================================================
-- REV53: M1/S1 AI autonomy safety and approval baseline (disabled by default)
-- =============================================================================
-- Adds the administrator-managed asset environment column and the four
-- autonomous-run domain tables. The feature stays disabled until
-- OGS_AI_AUTONOMY_ENABLED is set; these schema objects are inert meanwhile.
-- Safe to re-run: ALTER is guarded, tables use IF NOT EXISTS.

SET @col_exists := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_host'
      AND COLUMN_NAME = 'ai_environment'
);
SET @sql := IF(@col_exists = 0,
    'ALTER TABLE `t_host` ADD COLUMN `ai_environment` VARCHAR(10) NOT NULL DEFAULT ''production''',
    'SELECT "ai_environment exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

CREATE TABLE IF NOT EXISTS `t_ai_autonomous_run` (
  `id` varchar(32) NOT NULL,
  `owner` varchar(24) NOT NULL,
  `goal` varchar(512) NOT NULL,
  `host_id` int NOT NULL,
  `host_alias` varchar(25) NOT NULL,
  `system_user_id` int NOT NULL,
  `system_user_alias` varchar(24) NOT NULL,
  `mode` varchar(16) NOT NULL,
  `status` varchar(20) NOT NULL,
  `outcome` varchar(16) DEFAULT NULL,
  `revision` int NOT NULL DEFAULT 0,
  `budget_json` text NOT NULL,
  `latest_event_seq` int NOT NULL DEFAULT 0,
  `cancel_requested` tinyint(1) NOT NULL DEFAULT 0,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_auto_run_owner` (`owner`),
  KEY `idx_ai_auto_run_host` (`host_id`),
  KEY `idx_ai_auto_run_status` (`status`),
  KEY `idx_ai_auto_run_created_at` (`created_at`),
  KEY `idx_ai_auto_run_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `t_ai_autonomous_step` (
  `id` varchar(32) NOT NULL,
  `run_id` varchar(32) NOT NULL,
  `kind` varchar(16) NOT NULL,
  `status` varchar(20) NOT NULL,
  `seq` int NOT NULL,
  `summary` varchar(255) NOT NULL,
  `action_json` text DEFAULT NULL,
  `action_digest` varchar(64) DEFAULT NULL,
  `note` varchar(255) NOT NULL DEFAULT '',
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_autonomy_step_seq` (`run_id`, `seq`),
  KEY `idx_ai_auto_step_run` (`run_id`),
  KEY `idx_ai_auto_step_status` (`status`),
  KEY `idx_ai_auto_step_created_at` (`created_at`),
  KEY `idx_ai_auto_step_updated_at` (`updated_at`),
  CONSTRAINT `fk_ai_auto_step_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `t_ai_autonomous_event` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(32) NOT NULL,
  `sequence` int NOT NULL,
  `event_type` varchar(32) NOT NULL,
  `payload_json` text NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_autonomy_event_sequence` (`run_id`, `sequence`),
  KEY `idx_ai_auto_event_run` (`run_id`),
  CONSTRAINT `fk_ai_auto_event_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `t_ai_autonomous_artifact` (
  `id` varchar(32) NOT NULL,
  `run_id` varchar(32) NOT NULL,
  `step_id` varchar(32) DEFAULT NULL,
  `kind` varchar(32) NOT NULL,
  `title` varchar(128) NOT NULL,
  `content_ciphertext` longtext NOT NULL,
  `size_bytes` int NOT NULL DEFAULT 0,
  `truncated` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_auto_artifact_run` (`run_id`),
  KEY `idx_ai_auto_artifact_expiry` (`expires_at`),
  CONSTRAINT `fk_ai_auto_artifact_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
