-- M2 monitoring: admin-owned sources and explicit host identity mappings.
-- Monitoring observations remain bounded conversation data; no metrics/logs are stored here.

CREATE TABLE IF NOT EXISTS `t_ai_monitoring_source` (
  `id` int NOT NULL AUTO_INCREMENT,
  `name` varchar(64) NOT NULL,
  `source_type` varchar(16) NOT NULL,
  `base_url` varchar(255) NOT NULL,
  `token_ciphertext` varchar(2048) DEFAULT NULL,
  `verify_tls` tinyint(1) NOT NULL DEFAULT 1,
  `enabled` tinyint(1) NOT NULL DEFAULT 1,
  `created_by` varchar(24) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_monitoring_source_name` (`name`),
  KEY `idx_ai_monitoring_source_type` (`source_type`),
  KEY `idx_ai_monitoring_source_enabled` (`enabled`),
  KEY `idx_ai_monitoring_source_created_at` (`created_at`),
  KEY `idx_ai_monitoring_source_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE IF NOT EXISTS `t_ai_monitoring_host_mapping` (
  `id` int NOT NULL AUTO_INCREMENT,
  `source_id` int NOT NULL,
  `host_id` int NOT NULL,
  `external_ref_json` text NOT NULL,
  `confirmed_by` varchar(24) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_monitoring_source_host` (`source_id`,`host_id`),
  KEY `idx_ai_monitoring_mapping_source` (`source_id`),
  KEY `idx_ai_monitoring_mapping_host` (`host_id`),
  KEY `idx_ai_monitoring_mapping_created_at` (`created_at`),
  KEY `idx_ai_monitoring_mapping_updated_at` (`updated_at`),
  CONSTRAINT `fk_ai_monitoring_mapping_source` FOREIGN KEY (`source_id`)
    REFERENCES `t_ai_monitoring_source` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
