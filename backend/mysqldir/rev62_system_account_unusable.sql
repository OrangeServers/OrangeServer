-- v1.2.0: 中和存量实例的内置 system 账号口令。
--
-- 问题: orange.sql 曾把 system 的 password 播种为 base64 占位串, 注释称其
--   "不可登录", 但 verify_pwd 的 base64 兼容路径会解码并匹配成功 —— 该占位串
--   随仓库公开, 等于给一个 group='admin' 的内置账号配了人人皆知的口令。
--   首次部署向导不会修复它: bootstrap_db 仅在 system 行缺失时才创建, 而向导
--   apply 成功后不再运行, 因此存量实例升级后仍带着旧口令。
--
-- 修复: 改写为不可登录哨兵 —— 一次性随机明文的 bcrypt 摘要, 明文从未落盘且
--   不可恢复, 任何输入都无法匹配; 保留 bcrypt 开销, 不产生耗时差异泄露。
--   该字面量必须与 app/tools/basesec.py 的 UNUSABLE_PASSWORD_HASH 以及
--   orange.sql、rev47_h7_cron_owner_fk.sql 的播种值保持一致。
--
-- 幂等: WHERE 跳过已是哨兵值的行, 重复执行无副作用。
-- 不影响 t_cron.job_owner 外键: 只改口令, 不改 name/id, FK 目标行仍然存在。

SET NAMES utf8mb4;

UPDATE `t_acc_user`
SET `password` = '$2b$12$GrcI53JVdPLfQ/POoL9QBeuSC9lr2DgQ6MWaBHbGZWYSYRrcNiQ5K'
WHERE `name` = 'system'
  AND `password` <> '$2b$12$GrcI53JVdPLfQ/POoL9QBeuSC9lr2DgQ6MWaBHbGZWYSYRrcNiQ5K';

-- password_version 由 rev47_h9 引入; 更早的实例可能还没跑该迁移, 因此按
-- information_schema 守卫, 缺列时跳过而不是整条迁移失败 (沿用 rev47 的写法)。
SET @has_pv := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE()
      AND TABLE_NAME = 't_acc_user'
      AND COLUMN_NAME = 'password_version'
);
SET @sql := IF(@has_pv = 1,
    'UPDATE `t_acc_user` SET `password_version` = 2 WHERE `name` = ''system''',
    'SELECT "password_version 列不存在, 跳过 (先执行 rev47_h9_password_version.sql)" AS info');
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 验证: system 行必须已是 bcrypt 哨兵, 不能再有可解码的旧格式
SELECT IF(
    (SELECT COUNT(*) FROM `t_acc_user`
     WHERE `name` = 'system'
       AND `password` LIKE '$2b$12$%') = 1,
    'rev62 成功: 内置 system 账号已不可登录',
    'rev62 失败: system 行缺失或口令仍非 bcrypt 哨兵'
) AS migration_status;
