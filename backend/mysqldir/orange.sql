-- MySQL dump 10.13  Distrib 5.6.21-70.0, for Linux (x86_64)
--
-- Host: localhost    Database: orange1
-- ------------------------------------------------------
-- Server version	5.6.21-70.0-log

/*!40101 SET @OLD_CHARACTER_SET_CLIENT=@@CHARACTER_SET_CLIENT */;
/*!40101 SET @OLD_CHARACTER_SET_RESULTS=@@CHARACTER_SET_RESULTS */;
/*!40101 SET @OLD_COLLATION_CONNECTION=@@COLLATION_CONNECTION */;
/*!40101 SET NAMES utf8 */;
/*!40103 SET @OLD_TIME_ZONE=@@TIME_ZONE */;
/*!40103 SET TIME_ZONE='+00:00' */;
/*!40014 SET @OLD_UNIQUE_CHECKS=@@UNIQUE_CHECKS, UNIQUE_CHECKS=0 */;
/*!40014 SET @OLD_FOREIGN_KEY_CHECKS=@@FOREIGN_KEY_CHECKS, FOREIGN_KEY_CHECKS=0 */;
/*!40101 SET @OLD_SQL_MODE=@@SQL_MODE, SQL_MODE='NO_AUTO_VALUE_ON_ZERO' */;
/*!40111 SET @OLD_SQL_NOTES=@@SQL_NOTES, SQL_NOTES=0 */;

--
-- Table structure for table `t_acc_group`
--

DROP TABLE IF EXISTS `t_acc_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_acc_group` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `name` varchar(25) NOT NULL,
  `nums` int(10) NOT NULL,
  `remarks` varchar(30) DEFAULT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_t_acc_group_name` (`name`),  -- DEPLOY-AUDIT: FK 目标列必须有索引 (fk_ahug_group_name), 否则 initdb 报 1822
  KEY `idx_t_acc_group_is_deleted` (`is_deleted`)  -- REV47-M6: 软删除索引
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_acc_group`
--

LOCK TABLES `t_acc_group` WRITE;
/*!40000 ALTER TABLE `t_acc_group` DISABLE KEYS */;
-- DEPLOY-AUDIT P0-1: DDL 加列(is_deleted 等)后, 无列清单 INSERT 列数不匹配会让
--   mysql 容器 initdb 直接失败; 全部种子 INSERT 改为显式列清单。
INSERT INTO `t_acc_group` (`id`,`name`,`nums`,`remarks`) VALUES (1,'admin',1,'管理员组');
/*!40000 ALTER TABLE `t_acc_group` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for durable AI diagnostics (REV50)
--

DROP TABLE IF EXISTS `t_ai_diagnostic_report`;
DROP TABLE IF EXISTS `t_ai_diagnostic_evidence`;
DROP TABLE IF EXISTS `t_ai_diagnostic_event`;
DROP TABLE IF EXISTS `t_ai_diagnostic_run`;

CREATE TABLE `t_ai_diagnostic_run` (
  `id` varchar(32) NOT NULL,
  `owner` varchar(24) NOT NULL,
  `conversation_id` varchar(32) DEFAULT NULL,
  `profile_id` varchar(64) NOT NULL,
  `profile_name` varchar(128) NOT NULL,
  `status` varchar(16) NOT NULL,
  `target_count` int NOT NULL DEFAULT 0,
  `success_count` int NOT NULL DEFAULT 0,
  `failed_count` int NOT NULL DEFAULT 0,
  `system_user_id` int NOT NULL,
  `system_user_alias` varchar(24) NOT NULL,
  `parameters_json` text NOT NULL,
  `summary_json` text NOT NULL,
  `asset_progress_json` text NOT NULL,
  `latest_event_seq` int NOT NULL DEFAULT 0,
  `cancel_requested` tinyint(1) NOT NULL DEFAULT 0,
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `evidence_expires_at` datetime NOT NULL,
  `audit_expires_at` datetime NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_diag_run_owner` (`owner`),
  KEY `idx_ai_diag_run_conversation` (`conversation_id`),
  KEY `idx_ai_diag_run_profile` (`profile_id`),
  KEY `idx_ai_diag_run_status` (`status`),
  KEY `idx_ai_diag_run_system_user` (`system_user_id`),
  KEY `idx_ai_diag_run_evidence_expiry` (`evidence_expires_at`),
  KEY `idx_ai_diag_run_audit_expiry` (`audit_expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `t_ai_diagnostic_event` (
  `id` bigint NOT NULL AUTO_INCREMENT,
  `run_id` varchar(32) NOT NULL,
  `sequence` int NOT NULL,
  `event_type` varchar(32) NOT NULL,
  `payload_json` text NOT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_diagnostic_event_sequence` (`run_id`, `sequence`),
  KEY `idx_ai_diag_event_run` (`run_id`),
  CONSTRAINT `fk_ai_diag_event_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_diagnostic_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `t_ai_diagnostic_evidence` (
  `id` varchar(32) NOT NULL,
  `run_id` varchar(32) NOT NULL,
  `target_id` int DEFAULT NULL,
  `asset_alias` varchar(25) NOT NULL,
  `probe_id` varchar(64) NOT NULL,
  `title` varchar(128) NOT NULL,
  `kind` varchar(32) NOT NULL,
  `status` varchar(16) NOT NULL,
  `content_ciphertext` longtext NOT NULL,
  `error_ciphertext` longtext NOT NULL,
  `truncated` tinyint(1) NOT NULL DEFAULT 0,
  `collected_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_ai_diag_evidence_run` (`run_id`),
  KEY `idx_ai_diag_evidence_expiry` (`expires_at`),
  CONSTRAINT `fk_ai_diag_evidence_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_diagnostic_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `t_ai_diagnostic_report` (
  `run_id` varchar(32) NOT NULL,
  `status` varchar(16) NOT NULL,
  `severity` varchar(16) NOT NULL,
  `summary` text NOT NULL,
  `findings_json` text NOT NULL,
  `evidence_insufficient` tinyint(1) NOT NULL DEFAULT 0,
  `generated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `expires_at` datetime NOT NULL,
  PRIMARY KEY (`run_id`),
  KEY `idx_ai_diag_report_expiry` (`expires_at`),
  CONSTRAINT `fk_ai_diag_report_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_diagnostic_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

--
-- Table structure for M1/S1 AI autonomy (REV53, disabled by default)
--

DROP TABLE IF EXISTS `t_ai_autonomous_artifact`;
DROP TABLE IF EXISTS `t_ai_autonomous_event`;
DROP TABLE IF EXISTS `t_ai_autonomous_step`;
DROP TABLE IF EXISTS `t_ai_autonomous_run`;

CREATE TABLE `t_ai_autonomous_run` (
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
  `lease_owner` varchar(64) DEFAULT NULL,
  `lease_expires_at` datetime DEFAULT NULL,
  `heartbeat_at` datetime DEFAULT NULL,
  `graph_version` varchar(32) NOT NULL DEFAULT 'v1',
  `started_at` datetime DEFAULT NULL,
  `completed_at` datetime DEFAULT NULL,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_ai_auto_run_owner` (`owner`),
  KEY `idx_ai_auto_run_host` (`host_id`),
  KEY `idx_ai_auto_run_status` (`status`),
  KEY `idx_ai_auto_run_created_at` (`created_at`),
  KEY `idx_ai_auto_run_updated_at` (`updated_at`),
  KEY `idx_ai_auto_run_lease_expires` (`lease_expires_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `t_ai_autonomous_step` (
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
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_ai_autonomy_step_seq` (`run_id`, `seq`),
  KEY `idx_ai_auto_step_run` (`run_id`),
  KEY `idx_ai_auto_step_status` (`status`),
  KEY `idx_ai_auto_step_created_at` (`created_at`),
  KEY `idx_ai_auto_step_updated_at` (`updated_at`),
  CONSTRAINT `fk_ai_auto_step_run` FOREIGN KEY (`run_id`)
    REFERENCES `t_ai_autonomous_run` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;

CREATE TABLE `t_ai_autonomous_event` (
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

CREATE TABLE `t_ai_autonomous_artifact` (
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

--
-- Table structure for table `t_acc_user`
--

DROP TABLE IF EXISTS `t_acc_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_acc_user` (
  `id` int(11) unsigned NOT NULL AUTO_INCREMENT,
  `alias` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `name` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `group` varchar(24) NOT NULL,
  `password` varchar(255) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `password_version` int NOT NULL DEFAULT 2,
  `usrole` varchar(10) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,
  `mail` varchar(128) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL,  -- REV16 P2-4/MED-3: 24 -> 128
  `remarks` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_t_acc_user_name` (`name`),  -- REV45-H1/H2: name 加 unique 约束, 与 mail 一致
  UNIQUE KEY `uq_t_acc_user_mail` (`mail`),  -- REV16 P2-4/MED-3: mail 加 unique 索引
  KEY `idx_t_acc_user_is_deleted` (`is_deleted`),  -- REV47-M6: 软删除索引
  KEY `idx_acc_user_password_version` (`password_version`),
  KEY `idx_t_acc_user_created_at` (`created_at`),
  KEY `idx_t_acc_user_updated_at` (`updated_at`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_acc_user`
--

LOCK TABLES `t_acc_user` WRITE;
/*!40000 ALTER TABLE `t_acc_user` DISABLE KEYS */;
INSERT INTO `t_acc_user` (`id`,`alias`,`name`,`group`,`password`,`password_version`,`usrole`,`mail`,`remarks`) VALUES (1,'管理员','admin','admin','YWRtaW4=',1,'admin','admin@orange.com','超级管理员');
INSERT INTO `t_acc_user` (`id`,`alias`,`name`,`group`,`password`,`password_version`,`usrole`,`mail`,`remarks`) VALUES (99,'system','system','admin','J1FTX19fX19fX18=',1,'member','system@orange.local','[REV45-H7/R2-4] 内置系统账号, 作为 cron.job_owner FK 默认目标; 不可删除');
/*!40000 ALTER TABLE `t_acc_user` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_auth_host`
--

DROP TABLE IF EXISTS `t_auth_host`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_auth_host` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `name` varchar(25) NOT NULL,
  `remarks` varchar(255) DEFAULT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_t_auth_host_is_deleted` (`is_deleted`),  -- REV47-M6: 软删除索引
  KEY `idx_t_auth_host_created_at` (`created_at`),
  KEY `idx_t_auth_host_updated_at` (`updated_at`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_auth_host`
--

LOCK TABLES `t_auth_host` WRITE;
/*!40000 ALTER TABLE `t_auth_host` DISABLE KEYS */;
INSERT INTO `t_auth_host` (`id`,`name`,`remarks`) VALUES (1,'所有权限','管理员权限');
/*!40000 ALTER TABLE `t_auth_host` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_auth_host_user`
--

DROP TABLE IF EXISTS `t_auth_host_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_auth_host_user` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `auth_id` int(10) NOT NULL,
  `user_name` varchar(24) NOT NULL,            -- REV45-H5: 100 -> 24 (匹配 t_acc_user.name)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_auth_user` (`auth_id`,`user_name`),
  KEY `fk_ahu_auth_id` (`auth_id`),
  CONSTRAINT `fk_ahu_auth_id` FOREIGN KEY (`auth_id`) REFERENCES `t_auth_host` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `t_auth_host_user_group`
--

DROP TABLE IF EXISTS `t_auth_host_user_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_auth_host_user_group` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `auth_id` int(10) NOT NULL,
  `group_name` varchar(25) NOT NULL,           -- REV45-H4/H5: 100 -> 25 (匹配 t_acc_group.name)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_auth_user_group` (`auth_id`,`group_name`),
  KEY `fk_ahug_auth_id` (`auth_id`),
  KEY `fk_ahug_group_name` (`group_name`),    -- REV45-H4: FK 索引
  CONSTRAINT `fk_ahug_auth_id` FOREIGN KEY (`auth_id`) REFERENCES `t_auth_host` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_ahug_group_name` FOREIGN KEY (`group_name`) REFERENCES `t_acc_group` (`name`) ON DELETE CASCADE ON UPDATE CASCADE  -- GROUP-RENAME: FK
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `t_auth_host_host_group`
--

DROP TABLE IF EXISTS `t_auth_host_host_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_auth_host_host_group` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `auth_id` int(10) NOT NULL,
  `group_name` varchar(25) NOT NULL,           -- REV45-H4/H5: 100 -> 25 (匹配 t_group.name)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_auth_host_group` (`auth_id`,`group_name`),
  KEY `fk_ahhg_auth_id` (`auth_id`),
  KEY `fk_ahhg_group_name` (`group_name`),    -- REV45-H4: FK 索引
  CONSTRAINT `fk_ahhg_auth_id` FOREIGN KEY (`auth_id`) REFERENCES `t_auth_host` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_ahhg_group_name` FOREIGN KEY (`group_name`) REFERENCES `t_group` (`name`) ON DELETE CASCADE ON UPDATE CASCADE  -- GROUP-RENAME: FK
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `t_auth_host_sys_user`
--

DROP TABLE IF EXISTS `t_auth_host_sys_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_auth_host_sys_user` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `auth_id` int(10) NOT NULL,
  `sys_user_alias` varchar(24) NOT NULL,        -- REV45-H5: 100 -> 30 (匹配 t_sys_user.alias); REV47-M5: 30 -> 24 (匹配 t_acc_user.alias)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_auth_sys_user` (`auth_id`,`sys_user_alias`),
  KEY `fk_ahsu_auth_id` (`auth_id`),
  CONSTRAINT `fk_ahsu_auth_id` FOREIGN KEY (`auth_id`) REFERENCES `t_auth_host` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for auth junction tables
--

LOCK TABLES `t_auth_host_user` WRITE;
/*!40000 ALTER TABLE `t_auth_host_user` DISABLE KEYS */;
INSERT INTO `t_auth_host_user` VALUES (1,1,'admin');
/*!40000 ALTER TABLE `t_auth_host_user` ENABLE KEYS */;
UNLOCK TABLES;

LOCK TABLES `t_auth_host_user_group` WRITE;
/*!40000 ALTER TABLE `t_auth_host_user_group` DISABLE KEYS */;
INSERT INTO `t_auth_host_user_group` VALUES (1,1,'admin');
/*!40000 ALTER TABLE `t_auth_host_user_group` ENABLE KEYS */;
UNLOCK TABLES;

LOCK TABLES `t_auth_host_host_group` WRITE;
/*!40000 ALTER TABLE `t_auth_host_host_group` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_auth_host_host_group` ENABLE KEYS */;
UNLOCK TABLES;

LOCK TABLES `t_auth_host_sys_user` WRITE;
/*!40000 ALTER TABLE `t_auth_host_sys_user` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_auth_host_sys_user` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_command_log`
--

DROP TABLE IF EXISTS `t_command_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_command_log` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `log_name` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,  -- REVIEW-10-P1-2: 30->24; DEPLOY-AUDIT: 字符集须与 FK 目标 t_acc_user.name(utf8mb4) 一致, 否则 initdb 报 3780
  `log_type` varchar(30) NOT NULL,
  `log_info` varchar(255) NOT NULL,
  `log_host` varchar(30) DEFAULT NULL,           -- REVIEW-10-P1-2: 255 -> 30 + FK -> t_host.alias
  `log_status` varchar(32) NOT NULL,
  `log_reason` varchar(255) DEFAULT NULL,
  `log_time` datetime NOT NULL,
  `log_exit_code` int DEFAULT NULL,
  `log_duration_ms` int DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_clog_name` (`log_name`),              -- REVIEW-10-P1-3
  KEY `idx_clog_host` (`log_host`),              -- REVIEW-10-P1-3
  KEY `idx_clog_time` (`log_time`),              -- REVIEW-10-P1-3
  CONSTRAINT `fk_clog_user` FOREIGN KEY (`log_name`) REFERENCES `t_acc_user` (`name`) ON DELETE SET NULL,
  CONSTRAINT `fk_clog_host` FOREIGN KEY (`log_host`) REFERENCES `t_host` (`alias`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_command_log`
--

LOCK TABLES `t_command_log` WRITE;
/*!40000 ALTER TABLE `t_command_log` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_command_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_cron`
--

DROP TABLE IF EXISTS `t_cron`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_cron` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `job_name` varchar(30) NOT NULL,
  `job_minute` varchar(20) NOT NULL,
  `job_hour` varchar(20) NOT NULL,
  `job_day` varchar(20) NOT NULL,
  `job_month` varchar(20) NOT NULL,
  `job_week` varchar(20) NOT NULL,
  -- REVIEW-10-P2-5: job_hosts / job_groups 已迁移到 t_cron_host / t_cron_group, 列已删除
  `job_sys_user` varchar(255) NOT NULL,
  `job_command` varchar(255) NOT NULL,
  `job_status` varchar(20) NOT NULL,
  `job_remarks` varchar(255) DEFAULT NULL,
  `job_owner` varchar(30) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci NOT NULL DEFAULT 'system',  -- REV45-H7 (R2-4): cron 创建者, FK -> t_acc_user.name; DEPLOY-AUDIT: 对齐字符集(3780)
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `job_name` (`job_name`) USING HASH,
  KEY `fk_cron_owner` (`job_owner`),  -- REV45-H7 (R2-4): owner FK 索引
  KEY `idx_t_cron_is_deleted` (`is_deleted`),  -- REV47-M6: 软删除索引
  KEY `idx_t_cron_created_at` (`created_at`),
  KEY `idx_t_cron_updated_at` (`updated_at`),
  CONSTRAINT `fk_cron_owner` FOREIGN KEY (`job_owner`) REFERENCES `t_acc_user` (`name`) ON DELETE SET DEFAULT  -- REV45-H7 (R2-4): 删除用户时,其 cron 自动重置为 'system'
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_cron`
--

LOCK TABLES `t_cron` WRITE;
/*!40000 ALTER TABLE `t_cron` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_cron` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_cron_host`
--

DROP TABLE IF EXISTS `t_cron_host`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_cron_host` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `cron_id` int(10) NOT NULL,
  `host_alias` varchar(25) NOT NULL,            -- REV45-H6: 100 -> 25 (匹配 t_host.alias)
  PRIMARY KEY (`id`),
  KEY `fk_cron_host_cron_id` (`cron_id`),
  KEY `fk_cron_host_host_alias` (`host_alias`),  -- REV45-H6: FK 索引
  CONSTRAINT `fk_cron_host_cron_id` FOREIGN KEY (`cron_id`) REFERENCES `t_cron` (`id`) ON DELETE CASCADE,
  CONSTRAINT `fk_cron_host_host_alias` FOREIGN KEY (`host_alias`) REFERENCES `t_host` (`alias`) ON DELETE CASCADE  -- REV45-H6: FK
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Table structure for table `t_cron_group`
--

DROP TABLE IF EXISTS `t_cron_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_cron_group` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `cron_id` int(10) NOT NULL,
  `group_name` varchar(100) NOT NULL,
  PRIMARY KEY (`id`),
  KEY `fk_cron_group_cron_id` (`cron_id`),
  CONSTRAINT `fk_cron_group_cron_id` FOREIGN KEY (`cron_id`) REFERENCES `t_cron` (`id`) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for tables `t_cron_host` / `t_cron_group`
--

LOCK TABLES `t_cron_host` WRITE;
/*!40000 ALTER TABLE `t_cron_host` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_cron_host` ENABLE KEYS */;
UNLOCK TABLES;
LOCK TABLES `t_cron_group` WRITE;
/*!40000 ALTER TABLE `t_cron_group` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_cron_group` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_cz_log`
--

DROP TABLE IF EXISTS `t_cz_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_cz_log` (
  `id` int(10) NOT NULL AUTO_INCREMENT,
  `log_name` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,  -- REVIEW-10-P1-2: 30->24 + FK; DEPLOY-AUDIT: 对齐 t_acc_user.name 字符集(3780)
  `log_type` varchar(30) NOT NULL,
  `log_info` varchar(255) NOT NULL,
  `log_details` varchar(255) NOT NULL,
  `log_status` varchar(32) NOT NULL,
  `log_reason` varchar(255) DEFAULT NULL,
  `log_time` datetime NOT NULL ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_czlog_name` (`log_name`),             -- REVIEW-10-P1-3
  KEY `idx_czlog_time` (`log_time`),             -- REVIEW-10-P1-3
  CONSTRAINT `fk_czlog_user` FOREIGN KEY (`log_name`) REFERENCES `t_acc_user` (`name`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_cz_log`
--

LOCK TABLES `t_cz_log` WRITE;
/*!40000 ALTER TABLE `t_cz_log` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_cz_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_group`
--

DROP TABLE IF EXISTS `t_group`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_group` (
  `id` int(4) unsigned NOT NULL AUTO_INCREMENT,
  `name` varchar(25) NOT NULL,
  `nums` int(5) NOT NULL,
  `remarks` varchar(30) DEFAULT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_t_group_name` (`name`),  -- DEPLOY-AUDIT: 与 ORM 对齐, FK 目标列需索引
  KEY `idx_t_group_is_deleted` (`is_deleted`)  -- REV47-M6: 软删除索引
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_group`
--

LOCK TABLES `t_group` WRITE;
/*!40000 ALTER TABLE `t_group` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_group` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_host`
--

DROP TABLE IF EXISTS `t_host`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_host` (
  `id` int(4) unsigned NOT NULL AUTO_INCREMENT,
  `alias` varchar(25) NOT NULL,
  `host_ip` varchar(45) NOT NULL,           -- REVIEW-10-P1-4: 16 -> 45, 业务走 IPv6
  `host_port` int(5) NOT NULL,
  `group` varchar(25) DEFAULT NULL,         -- REV45-H3: 20 -> 25 (匹配 t_group.name)
  `ai_environment` varchar(10) NOT NULL DEFAULT 'production',  -- REV53: M1/S1 自治环境 (production|staging|lab), 仅管理员可改
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `alias` (`alias`),
  KEY `idx_host_ip` (`host_ip`),              -- REVIEW-10-P1-4: host_ip 加 index
  KEY `fk_host_group` (`group`),              -- REV45-H3: FK 索引
  KEY `idx_t_host_is_deleted` (`is_deleted`),  -- REV47-M6: 软删除索引
  KEY `idx_t_host_created_at` (`created_at`),
  KEY `idx_t_host_updated_at` (`updated_at`),
  CONSTRAINT `fk_host_group` FOREIGN KEY (`group`) REFERENCES `t_group` (`name`) ON DELETE SET NULL ON UPDATE CASCADE  -- GROUP-RENAME: FK
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_host`
--

LOCK TABLES `t_host` WRITE;
/*!40000 ALTER TABLE `t_host` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_host` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_line_chart`
--

DROP TABLE IF EXISTS `t_line_chart`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_line_chart` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `chart_date` date DEFAULT NULL,
  `login_count` int(255) DEFAULT NULL,
  `logerr_count` int(255) DEFAULT NULL,
  `user_count` int(255) DEFAULT NULL,
  `log_name` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_t_line_chart_log_name` (`log_name`),
  CONSTRAINT `fk_lc_log_name` FOREIGN KEY (`log_name`) REFERENCES `t_acc_user` (`name`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_line_chart`
--

LOCK TABLES `t_line_chart` WRITE;
/*!40000 ALTER TABLE `t_line_chart` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_line_chart` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_login_log`
--

DROP TABLE IF EXISTS `t_login_log`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_login_log` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `log_name` varchar(24) CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci DEFAULT NULL,  -- REVIEW-10-P1-2: 30->24 + FK; DEPLOY-AUDIT: 对齐 t_acc_user.name 字符集(3780)
  `log_nw_ip` varchar(45) NOT NULL,              -- REVIEW-10-P1-1: 20 -> 45, IPv6 兼容
  `log_gw_ip` varchar(45) DEFAULT NULL,          -- REVIEW-10-P1-1
  `log_gw_cs` varchar(45) DEFAULT NULL,          -- REVIEW-10-P1-1
  `log_agent` varchar(255) NOT NULL,
  `log_status` varchar(255) NOT NULL,            -- REVIEW-10-P1-5: 10 -> 255, 同步 ORM 扩展
  `log_reason` varchar(30) DEFAULT NULL,
  `log_time` datetime NOT NULL,
  `log_session_id` varchar(64) DEFAULT NULL,
  `log_csrf_nonce` varchar(64) DEFAULT NULL,
  PRIMARY KEY (`id`),
  KEY `idx_llog_name` (`log_name`),              -- REVIEW-10-P1-3
  KEY `idx_llog_time` (`log_time`),              -- REVIEW-10-P1-3
  CONSTRAINT `fk_llog_user` FOREIGN KEY (`log_name`) REFERENCES `t_acc_user` (`name`) ON DELETE SET NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_login_log`
--

LOCK TABLES `t_login_log` WRITE;
/*!40000 ALTER TABLE `t_login_log` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_login_log` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_ai_provider`
--

DROP TABLE IF EXISTS `t_ai_provider`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8mb4 */;
CREATE TABLE `t_ai_provider` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `provider_code` varchar(32) NOT NULL,
  `base_url` varchar(255) NOT NULL,
  `model` varchar(128) NOT NULL DEFAULT '',
  `context_window_tokens` int(11) NOT NULL DEFAULT '262144',
  `api_key_ciphertext` varchar(1024) DEFAULT NULL,
  `enabled` tinyint(1) NOT NULL DEFAULT '0',
  `is_default` tinyint(1) NOT NULL DEFAULT '0',
  `extra_body_json` text,
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP
    ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  UNIQUE KEY `uq_t_ai_provider_code` (`provider_code`),
  KEY `idx_t_ai_provider_code` (`provider_code`),
  KEY `idx_t_ai_provider_enabled` (`enabled`),
  KEY `idx_t_ai_provider_is_default` (`is_default`),
  KEY `idx_t_ai_provider_created_at` (`created_at`),
  KEY `idx_t_ai_provider_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_ai_provider`
--

LOCK TABLES `t_ai_provider` WRITE;
/*!40000 ALTER TABLE `t_ai_provider` DISABLE KEYS */;
INSERT INTO `t_ai_provider`
  (`provider_code`, `base_url`, `model`, `enabled`, `is_default`)
VALUES
  ('openai', 'https://api.openai.com/v1', '', 0, 0),
  ('deepseek', 'https://api.deepseek.com', '', 0, 0),
  ('minimax', 'https://api.minimaxi.com/v1', '', 0, 0),
  ('kimi', 'https://api.moonshot.cn/v1', '', 0, 0),
  ('qwen', 'https://dashscope.aliyuncs.com/compatible-mode/v1', '', 0, 0),
  ('glm', 'https://open.bigmodel.cn/api/paas/v4/', '', 0, 0),
  ('siliconflow', 'https://api.siliconflow.cn/v1', '', 0, 0);
/*!40000 ALTER TABLE `t_ai_provider` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_settings`
--

DROP TABLE IF EXISTS `t_settings`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_settings` (
  `id` int(11) NOT NULL AUTO_INCREMENT,
  `name` varchar(20) NOT NULL,
  `login_time` int(5) NOT NULL DEFAULT 3,
  `register_status` varchar(5) NOT NULL DEFAULT 'on',
  `color_matching` varchar(10) NOT NULL DEFAULT 'black',
  `login_fail_limit` int(11) NOT NULL DEFAULT 5,
  `lock_duration` int(11) NOT NULL DEFAULT 30,
  `password_expire_days` int(11) NOT NULL DEFAULT 90,
  `mfa_enabled` varchar(5) NOT NULL DEFAULT 'off',
  `password_complexity` varchar(5) NOT NULL DEFAULT 'off',
  `ssh_timeout` int(11) NOT NULL DEFAULT 30,
  `terminal_scrollback` int(11) NOT NULL DEFAULT 10000,
  `session_record` varchar(5) NOT NULL DEFAULT 'on',
  `max_concurrent_sessions` int(11) NOT NULL DEFAULT 3,
  `log_retention_days` int(11) NOT NULL DEFAULT 180,
  `command_audit` varchar(5) NOT NULL DEFAULT 'on',
  `upload_size_limit` int(11) NOT NULL DEFAULT 500,
  `allow_upload` varchar(5) NOT NULL DEFAULT 'on',
  `allow_download` varchar(5) NOT NULL DEFAULT 'on',
  `mail_notify` varchar(5) NOT NULL DEFAULT 'off',
  `alert_email` varchar(100) DEFAULT '',
  `mail_smtp_host` varchar(253) DEFAULT NULL,
  `mail_smtp_port` int(11) DEFAULT NULL,
  `mail_smtp_security` varchar(10) DEFAULT NULL,
  `mail_from` varchar(254) DEFAULT NULL,
  `mail_password_encrypted` text DEFAULT NULL,
  `system_name` varchar(50) NOT NULL DEFAULT 'OrangeServer',
  `login_notice` varchar(255) DEFAULT '',
  `language` varchar(10) NOT NULL DEFAULT 'zh-CN',  -- I18N (rev51): 界面语言 zh-CN | en-US
  PRIMARY KEY (`id`)
) ENGINE=InnoDB AUTO_INCREMENT=2 DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_settings`
--

LOCK TABLES `t_settings` WRITE;
/*!40000 ALTER TABLE `t_settings` DISABLE KEYS */;
INSERT INTO `t_settings` (`id`,`name`,`login_time`,`register_status`,`color_matching`) VALUES (1,'default',3,'off','black');
/*!40000 ALTER TABLE `t_settings` ENABLE KEYS */;
UNLOCK TABLES;

--
-- Table structure for table `t_sys_user`
--

DROP TABLE IF EXISTS `t_sys_user`;
/*!40101 SET @saved_cs_client     = @@character_set_client */;
/*!40101 SET character_set_client = utf8 */;
CREATE TABLE `t_sys_user` (
  `id` int(4) unsigned NOT NULL AUTO_INCREMENT,
  `alias` varchar(24) NOT NULL,
  `host_user` varchar(25) NOT NULL,
  `host_password` varchar(512) DEFAULT NULL,
  `host_key` varchar(255) DEFAULT NULL,
  `agreement` varchar(10) NOT NULL,
  `remarks` varchar(30) DEFAULT NULL,
  `is_deleted` tinyint(1) NOT NULL DEFAULT '0',  -- REV47-M6: 软删除标志 (0=正常 1=已软删)
  `created_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP,
  `updated_at` datetime NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`id`),
  KEY `idx_t_sys_user_is_deleted` (`is_deleted`),  -- REV47-M6: 软删除索引
  KEY `idx_t_sys_user_created_at` (`created_at`),
  KEY `idx_t_sys_user_updated_at` (`updated_at`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8;
/*!40101 SET character_set_client = @saved_cs_client */;

--
-- Dumping data for table `t_sys_user`
--

LOCK TABLES `t_sys_user` WRITE;
/*!40000 ALTER TABLE `t_sys_user` DISABLE KEYS */;
/*!40000 ALTER TABLE `t_sys_user` ENABLE KEYS */;
UNLOCK TABLES;
/*!40103 SET TIME_ZONE=@OLD_TIME_ZONE */;

/*!40101 SET SQL_MODE=@OLD_SQL_MODE */;
/*!40014 SET FOREIGN_KEY_CHECKS=@OLD_FOREIGN_KEY_CHECKS */;
/*!40014 SET UNIQUE_CHECKS=@OLD_UNIQUE_CHECKS */;
/*!40101 SET CHARACTER_SET_CLIENT=@OLD_CHARACTER_SET_CLIENT */;
/*!40101 SET CHARACTER_SET_RESULTS=@OLD_CHARACTER_SET_RESULTS */;
/*!40101 SET COLLATION_CONNECTION=@OLD_COLLATION_CONNECTION */;
/*!40111 SET SQL_NOTES=@OLD_SQL_NOTES */;

-- Dump completed on 2022-04-01 15:24:16
