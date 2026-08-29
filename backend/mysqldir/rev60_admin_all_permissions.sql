-- M2: backfill every active administrator into the existing all-access rule.
-- Idempotent: uq_auth_user(auth_id, user_name) makes repeated runs harmless.

SET NAMES utf8mb4;

INSERT IGNORE INTO `t_auth_host_user` (`auth_id`, `user_name`)
SELECT all_auth.`id`, admin_user.`name`
FROM `t_auth_host` AS all_auth
JOIN `t_acc_user` AS admin_user
  ON admin_user.`usrole` = 'admin'
 AND admin_user.`is_deleted` = 0
WHERE all_auth.`name` = '所有权限'
  AND all_auth.`is_deleted` = 0;
