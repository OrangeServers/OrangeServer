-- =============================================================================
-- REV56: M1/S3 autonomy Evidence references
-- =============================================================================
-- Adds t_ai_autonomous_evidence: bounded, redacted references that normalize
-- action/verification observations. Evidence stays marked untrusted (trusted
-- is always 0); large output remains in encrypted Artifacts, and conclusions
-- may only cite Evidence IDs belonging to the same Run. The feature stays
-- disabled until OGS_AI_AUTONOMY_ENABLED is set; this table is inert
-- meanwhile. Safe to re-run: the CREATE is guarded.

SET @tbl_exists := (
    SELECT COUNT(*) FROM information_schema.TABLES
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_ai_autonomous_evidence'
);
SET @sql := IF(@tbl_exists = 0,
    'CREATE TABLE `t_ai_autonomous_evidence` (
  `id` varchar(32) NOT NULL,
  `run_id` varchar(32) NOT NULL,
  `step_id` varchar(32) DEFAULT NULL,
  `kind` varchar(32) NOT NULL,
  `summary` varchar(500) NOT NULL,
  `artifact_ids_json` text NOT NULL,
  `trusted` tinyint(1) NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_auto_evidence_run` (`run_id`),
  CONSTRAINT `fk_ai_auto_evidence_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4',
    'SELECT "t_ai_autonomous_evidence exists" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;
