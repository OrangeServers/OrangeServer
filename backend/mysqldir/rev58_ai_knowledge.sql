-- M2/S2: reviewed Markdown knowledge truth plus embedding/index state.
-- Redis chunks and vectors remain derived data and can be rebuilt.

CREATE TABLE IF NOT EXISTS `t_ai_embedding_config` (
  `id` int NOT NULL,
  `provider_type` varchar(24) NOT NULL DEFAULT 'local',
  `base_url` varchar(255) DEFAULT NULL,
  `model` varchar(128) NOT NULL,
  `api_key_ciphertext` varchar(1024) DEFAULT NULL,
  `dimension` int NOT NULL DEFAULT 512,
  `model_fingerprint` varchar(64) NOT NULL,
  `indexed_fingerprint` varchar(64) DEFAULT NULL,
  `index_state` varchar(16) NOT NULL DEFAULT 'empty',
  `indexed_chunks` int NOT NULL DEFAULT 0,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_embedding_created_at` (`created_at`),
  KEY `idx_ai_embedding_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

INSERT IGNORE INTO `t_ai_embedding_config` (
  `id`,`provider_type`,`base_url`,`model`,`api_key_ciphertext`,`dimension`,
  `model_fingerprint`,`indexed_fingerprint`,`index_state`,`indexed_chunks`
) VALUES (
  1,'local',NULL,'BAAI/bge-small-zh-v1.5',NULL,512,
  '09b6e5dccb3cf9c17b68c4493ceb1cf6eb4c6980e8a429a8c3343d46932e75ec',
  NULL,'empty',0
);

CREATE TABLE IF NOT EXISTS `t_ai_knowledge_document` (
  `id` varchar(32) NOT NULL,
  `title` varchar(128) NOT NULL,
  `source_type` varchar(16) NOT NULL,
  `source_ref` varchar(64) DEFAULT NULL,
  `scope` varchar(128) NOT NULL DEFAULT 'global',
  `content` longtext NOT NULL,
  `content_sha256` varchar(64) NOT NULL,
  `version` int NOT NULL DEFAULT 1,
  `approved` tinyint(1) NOT NULL DEFAULT 1,
  `indexed_fingerprint` varchar(64) DEFAULT NULL,
  `chunk_count` int NOT NULL DEFAULT 0,
  `created_by` varchar(24) NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_knowledge_source` (`source_type`,`source_ref`),
  KEY `idx_ai_knowledge_source_type` (`source_type`),
  KEY `idx_ai_knowledge_approved` (`approved`),
  KEY `idx_ai_knowledge_created_by` (`created_by`),
  KEY `idx_ai_knowledge_created_at` (`created_at`),
  KEY `idx_ai_knowledge_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
