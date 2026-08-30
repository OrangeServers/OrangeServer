import hashlib, secrets, string, time, hashlib, os
from flask import request, jsonify, make_response
# REV38-M6: 统一 API 响应格式, 替换散落 jsonify({'code': ...})
from app.tools.apierr import api_error, api_response, ApiCode
from app.tools.basesec import hash_pwd, verify_pwd, needs_rehash, dummy_verify_pwd
from app.local.Captcha import verify_captcha
from app.tools.SqlListTool import ListTool
from app.tools.sendmail import _validate_email, InvalidEmailError
from app.mail.config import build_mailer, resolve_mail_configuration
from app.tools.redisdb import ConnRedis
from app.core.db.database import t_acc_user, t_acc_group, t_settings, db
from app.core.db.insert import osql_in
from app.tools.audlog import LoginToolsLog, CzToolsLog
from app.core.config import _env, LOGIN_FAIL_LIMIT_IP, MAIL_VERIFY_PREFIX, MAIL_VERIFY_TTL
from app.tools.at import Log, get_current_user, get_current_user_role, request_param
from app.tools.auto_update import AuthAutoUpdate, sync_user_permissions


# REV41-H1: 类内 admin 鉴权防御 (防 init.py 路由层误删 roles=[] 装饰器)
#   即使路由层已挂 roles=['admin'], 类内仍要做一次角色校验作为兜底
#   使用方式: class __init__ 第一行调用 _require_admin_or_raise()
def _require_admin_or_raise():
    """检查当前请求用户角色是否为 admin, 否则抛 PermissionError (HTTP 500 -> 修复为 403)."""
    role = get_current_user_role()
    if role != 'admin':
        Log.logger.warning('admin-only action blocked: role=%s', role)
        raise PermissionError('admin role required, got: %r' % role)


# REVIEW-13 P1-4: 验证码 SHA256 hash 存储 (Redis dump 不可逆读出明文)
def _hash_verify_code(code):
    return hashlib.sha256(code.encode('utf-8') if isinstance(code, str) else b'').hexdigest()


# CRIT-4 辅助：判断是否为生产 HTTPS 环境。Secure cookie 仅 HTTPS 下发送。
# OGS_HTTPS=true 启用（如 Nginx 代理 https 到后端 http）。
# 默认 false（开发环境，http://localhost 不发 secure cookie）。
def _is_prod_https():
    return _env('OGS_HTTPS', '').lower() in ('1', 'true', 'yes', 'on')


# CRIT-6：验证码限流（防邮件轰炸/邮箱枚举）
#   - 同一邮箱 60s 内最多 1 次（最小间隔）
#   - 同一邮箱 24h 内最多 10 次
#   - 同一 IP 24h 内最多 30 次
#   返回 (allowed: bool, retry_after_sec: int, error_code: int)
def _check_captcha_rate_limit(ords, email, ip):
    try:
        last_key = f'cap_last:{email}'
        last_ts = ords.conn.get(last_key)
        if last_ts:
            return False, 60, 109
        day_key = f'cap_day:{email}'
        day_count = ords.conn.incr(day_key)
        if day_count == 1:
            ords.conn.expire(day_key, 24 * 60 * 60)
        if day_count > 10:
            ttl = ords.conn.ttl(day_key)
            return False, max(ttl, 60), 110
        if ip:
            ip_key = f'cap_ip:{ip}'
            ip_count = ords.conn.incr(ip_key)
            if ip_count == 1:
                ords.conn.expire(ip_key, 24 * 60 * 60)
            if ip_count > 30:
                ttl = ords.conn.ttl(ip_key)
                return False, max(ttl, 60), 111
        ords.conn.set(last_key, '1', ex=60)
        return True, 0, 0
    except Exception:
        return True, 0, 0


class AccUserList:
    def __init__(self):
        self.lt = ListTool
        self.ords = ConnRedis()

    @property
    def acc_user_alias(self):
        user_token = request.cookies.get('ogs_token')
        # REV20-LOW-3: user_token 可能为 None, 需判空避免 Redis TypeError
        # 且 acc_user_name 为 None 会污染审计日志
        acc_user_name = self.ords.conn.get(user_token) if user_token else None
        # acc_user_name = request_param("name")
        log_msg = 'req_body: [ name=%s ] /account/user/alias' % acc_user_name
        try:
            if not acc_user_name:
                # 无有效登录身份，直接返回空响应
                return jsonify({"acc_user_list_msg": 'select list msg error'})
            user_alias = self.ords.conn.get(acc_user_name + '_alias')
            return jsonify({'alias': user_alias, 'username': acc_user_name})
        except (IOError, TypeError) as e:
            # REV20-LOW-3: TypeError (来自 None+str 拼接) 也需记录, 不入 try 范围后静默
            Log.logger.info(log_msg + ' "fail select list msg error" err=%s' % e)
            return jsonify({"acc_user_list_msg": 'select list msg error'})

    @property
    def acc_user_list(self):
        acc_user_type = request_param('user_type')
        log_msg = 'req_body: [ user_type=%s ] /account/user/list' % acc_user_type
        try:
            if acc_user_type == 'user_list':
                acc_user_id = request_param("id")
                # REV47-M6: 业务查询过滤软删行
                query_msg = t_acc_user.query.filter_by(id=acc_user_id, is_deleted=False).first()
                list_msg = self.lt.dict_reset_pop_auto(query_msg, 'password')
                return jsonify(list_msg)
            elif acc_user_type == 'user_info':
                acc_user_name = request_param("name")
                # REV47-M6: 业务查询过滤软删行
                query_msg = t_acc_user.query.filter_by(name=acc_user_name, is_deleted=False).first()
                list_msg = self.lt.dict_reset_pop_auto(query_msg, 'password')
                return jsonify(list_msg)
        except IOError:
            Log.logger.info(log_msg + ' "fail select list msg error"')
            # REV38-M6
            return api_error(ApiCode.CRON_INNER_ERROR, '服务器内部错误')

    @property
    def acc_user_auth_list(self):
        user_token = request.cookies.get('ogs_token')
        acc_user_name = self.ords.conn.get(user_token)
        log_msg = 'req_body: [ name=%s ] /account/user/auth_list' % acc_user_name
        try:
            user_role = acc_user_name + '_role'
            role = self.ords.conn.get(user_role)
            # REV38-M6
            return api_response(data={'usrole': role}, code=ApiCode.OK, msg='ok')
        except IOError:
            Log.logger.info(log_msg + ' "fail select list msg error"')
            # REV38-M6
            return api_error(ApiCode.CRON_INNER_ERROR, '服务器内部错误')
        except TypeError:
            Log.logger.info(log_msg + ' "fail name is none error"')
            # REV38-M6
            return api_error(ApiCode.USER_TYPE_ERROR, '操作失败 (code=211)')

    @property
    def acc_user_list_all(self):
        log_msg = 'req_body: [ None ] /account/user/list_all'
        try:
            # REV47-M6: 业务查询过滤软删行
            base_q = t_acc_user.query.filter_by(is_deleted=False)
            return ListTool.paginated_query(base_q, 'acc_user_list_msg', 'acc_user_len_msg', pop_fields=('password',))
        except IOError:
            Log.logger.info(log_msg + ' "fail select list msg error"')
            # REV38-M6
            return api_error(ApiCode.CRON_INNER_ERROR, '服务器内部错误')


class CheckMail:
    def __init__(self):
        self.email = request_param('email')
        self.ords = ConnRedis()
        self.user_nw_ip = request.headers.get('X-Real-IP', '127.0.0.1')

    def send(self):
        # P0-3: 邮箱格式校验 (防垃圾 / SMTP 资源耗尽 / Redis key 污染)
        try:
            _validate_email(self.email)
        except InvalidEmailError as e:
            Log.logger.info('req_body: [ email=%s ] /mail/send_user_mail invalid email: %s' % (self.email, e))
            return jsonify({'code': 100, 'msg': '邮箱格式不正确'})
        mail_config = resolve_mail_configuration()
        if mail_config is None:
            return jsonify({'code': 100, 'msg': '管理员尚未配置邮件服务'})
        sendmail = build_mailer(mail_config)

        mail_chk = t_acc_user.query.filter_by(mail=self.email).first()
        log_msg = 'req_body: [ email=%s ] /mail/send_user_mail' % self.email
        if not mail_chk:
            # CRIT-6 限流检查（注册验证码）
            allowed, retry_sec, err_code = _check_captcha_rate_limit(
                self.ords, self.email, self.user_nw_ip)
            if not allowed:
                return jsonify({'code': err_code, 'msg': f'请求过于频繁，请 {retry_sec} 秒后重试',
                                'retry_after': retry_sec})
            # P0-3: Redis key 加 prefix 防与其他业务 key 冲突
            redis_key = MAIL_VERIFY_PREFIX + self.email
            existing = self.ords.conn.get(redis_key)
            if existing:
                # P0-3: 重复发送不重新生成验证码, 避免攻击者反复覆盖重置有效窗口
                return jsonify({'code': 0, 'msg': '验证码仍有效, 请查收邮箱'})
            # P0-LOW-1: secrets 密码学安全随机数 (防 random 模块可预测)
            #   原: random.sample → Mersenne Twister, 攻击者观察足够多样本后可预测后续输出
            #   改: secrets.choice → CSPRNG, 不可预测, 适合安全令牌/验证码
            mail_verification = ''.join(secrets.choice(string.digits) for _ in range(6))
            msg = ("您在OrangeServer上注册账号的的验证码是   %s   验证码有效期3分钟，请在3分钟内完成注册" % mail_verification)
            # P1-4: SHA256 哈希后存 Redis (Redis dump 泄露不可逆读出明文)
            self.ords.conn.set(redis_key, _hash_verify_code(mail_verification), ex=MAIL_VERIFY_TTL)
            sendmail.send(self.email, 'OrangeServer', '注册验证码', msg)
            return jsonify({'code': 0})
        else:
            Log.logger.info(log_msg + ' \"fail\"')
            return jsonify({'code': 100, 'msg': '邮箱已被注册'})


class CheckUser:
    def __init__(self):
        self.username = request_param('username')
        self.captcha_id = request_param('captcha_id')
        self.captcha_answer = request_param('captcha_answer')
        self.user_nw_ip = request.headers.get('X-Real-IP', '127.0.0.1')
        self.ords = ConnRedis()

    def check(self):
        # REV16 P1-1/MED-1: chk_username 加 IP 限流 + captcha 校验
        # 背景: 路由 need_auth=False, 原本可被脚本批量撞库枚举系统内有效用户名
        #   (后续用于密码爆破 / 钓鱼)。修复:
        #   1. 仅在 captcha 校验通过后才返回查重结果
        #   2. 加 IP 维度计数限流 (10/min) 防脚本刷量
        #   3. 无论 username 是否存在, 文案统一为 "验证码错误或已过期",
        #      防攻击者通过响应差异反向枚举用户名存在性
        try:
            if not verify_captcha(self.ords, self.captcha_id, self.captcha_answer):
                return jsonify({'code': 100, 'msg': '验证码错误或已过期'})
        except Exception:
            return jsonify({'code': 100, 'msg': '验证码错误或已过期'})

        # IP 维度计数限流 (同一 IP 60s 内最多 10 次查重)
        try:
            ip_key = f'chk_user_ip:{self.user_nw_ip}'
            n = self.ords.conn.incr(ip_key)
            if n == 1:
                self.ords.conn.expire(ip_key, 60)
            if n > 10:
                ttl = self.ords.conn.ttl(ip_key)
                return jsonify({
                    'code': 110,
                    'msg': f'请求过于频繁，请 {max(ttl, 1)} 秒后重试',
                    'retry_after': max(ttl, 1),
                })
        except Exception:
            pass  # Redis 不可用不阻断业务

        # REV47-M6: 软删 username 视为可用 (让用户回收 name)
        user_chk = t_acc_user.query.filter_by(name=self.username, is_deleted=False).first()
        log_msg = 'req_body: [ username=%s ] /account/chk_username' % self.username
        if user_chk:
            Log.logger.info(log_msg + ' "fail"')
            return jsonify({'code': 100, 'msg': '用户名已存在'})
        else:
            return jsonify({'code': 0})


class UserLogin2(CheckUser, LoginToolsLog):
    def __init__(self):
        CheckUser.__init__(self)
        LoginToolsLog.__init__(self)
        self.password = request_param('password')
        self.user_nw_ip = request.headers.get('X-Real-IP', '127.0.0.1')
        self.user_agent = request.headers.get('User-Agent', '')

        self.ords = ConnRedis()

    def login_dl(self):
        # 1) 读取安全设置（阈值/锁定时长），Redis 不可用或设置缺失时降级为安全默认值
        try:
            user_setting = t_settings.query.filter_by(name='default').first()
            fail_limit_user = int(getattr(user_setting, 'login_fail_limit', 5) or 5)
            # P1-3: IP 维度阈值独立 (防 NAT 共享 IP 误锁)，优先从 t_settings 读，缺则从 conf 默认
            fail_limit_ip = int(getattr(user_setting, 'login_fail_limit_ip', LOGIN_FAIL_LIMIT_IP) or LOGIN_FAIL_LIMIT_IP)
            lock_dur = int(getattr(user_setting, 'lock_duration', 30) or 30)
            exp_date = (int(getattr(user_setting, 'login_time', 3) or 3)) * 60 * 60
        except Exception:
            fail_limit_user, fail_limit_ip, lock_dur, exp_date = 5, LOGIN_FAIL_LIMIT_IP, 30, 3 * 60 * 60

        # P1-5: 后端校验图形验证码（不依赖前端）
        #   原架构: 前端 canvas 画 + 前端比对 → 攻击者绕过前端 curl 即可跳过
        #   新架构: 后端生成 6 位字符串 → Redis captcha:<id> (TTL 180s)
        #           登录时多传 captcha_id + captcha_answer → 后端先 delete 再返回
        #   注: verify_captcha 本身防重放 (成功后立即 delete)
        captcha_id = request_param('captcha_id')
        captcha_answer = request_param('captcha_answer')
        if not verify_captcha(self.ords, captcha_id, captcha_answer):
            return jsonify({'code': 100, 'msg': '验证码错误或已过期'})

        # 2) 账号/IP 维度锁定检查：在被禁期间直接拒绝（避免泄露用户存在性）
        if self.username and self.ords.conn.get(f'login_lock:{self.username}'):
            return jsonify({'code': 100, 'msg': '账号已锁定，请稍后再试'})
        if self.user_nw_ip and self.ords.conn.get(f'login_lock_ip:{self.user_nw_ip}'):
            return jsonify({'code': 100, 'msg': '登录尝试过多，请稍后再试'})

        # 日志中**绝不**记录明文密码/验证码。username OK 用于审计
        log_msg = 'req_body: [ username=%s ] /account/login_dl' % self.username
        # REV47-M6: 软删用户不能登录 (含 is_deleted=False 过滤)
        #   防御: 即便密码匹配, 软删用户也直接走"用户名无效"路径, 不泄漏差异
        user_info = t_acc_user.query.filter_by(name=self.username, is_deleted=False).first()
        user_gw_ip = request_param('user_gw_ip')
        user_gw_cs = request_param('user_gw_cs')
        if user_info is not None:
            matched, is_legacy = verify_pwd(self.password, user_info.password)
            if matched:
                # 透明升级：旧 base64 密码或 rounds 不足时，登录成功后自动 rehash 为 bcrypt
                if is_legacy or needs_rehash(user_info.password):
                    try:
                        user_info.password = hash_pwd(self.password)
                        db.session.commit()
                    except Exception:
                        db.session.rollback()

                # CRIT-4: 用 make_response + set_cookie 设置安全 cookie
                # usedforsecurity=False: token 只需要 uniqueness, 不需密码学强度
                user_token = hashlib.sha1(os.urandom(24), usedforsecurity=False).hexdigest()
                role_name = self.username + '_role'
                self.ords.conn.set(user_token, self.username)
                self.ords.conn.expire(user_token, exp_date)
                self.ords.conn.set(role_name, user_info.usrole)
                self.ords.conn.set(self.username + '_alias', user_info.alias)
                # 登录成功：清零账号与 IP 的失败计数
                self.ords.conn.delete(f'login_fail:{self.username}')
                if self.user_nw_ip:
                    self.ords.conn.delete(f'login_fail_ip:{self.user_nw_ip}')
                self.host_log(self.username, self.user_nw_ip, user_gw_ip, user_gw_cs, self.user_agent, '成功')
                # CRIT-4：用 make_response + set_cookie 设置安全 cookie
                #   httponly=True: 防 XSS 偷 token
                #   samesite='Lax': 防 CSRF（同站/跨 GET 仍可带）
                #   secure=生产环境决定：HTTPS 才发
                # HIGH-9：同时设 csrf_token cookie（非 HttpOnly，前端可读）
                # REVIEW-6-P1-3：登录后写入 per-session 随机 nonce，csrf_token 携带 nonce 双因子
                from app.tools.csrf import make_csrf_token, set_csrf_nonce
                csrf_nonce = set_csrf_nonce(user_token)
                csrf_token = make_csrf_token(user_token, csrf_nonce)
                response = make_response(jsonify({'code': 0, 'token': user_token}))
                response.set_cookie(
                    'ogs_token',
                    value=user_token,
                    max_age=exp_date,
                    httponly=True,
                    secure=_is_prod_https(),
                    samesite='Lax',
                    path='/',
                )
                response.set_cookie(
                    'csrf_token',
                    value=csrf_token,
                    max_age=exp_date,
                    httponly=False,  # 前端 JS 要读
                    secure=_is_prod_https(),
                    samesite='Lax',
                    path='/',
                )
                return response
            else:
                # 密码错误：增加失败计数，达到阈值则锁定账号 + IP
                self._bump_login_fail(self.username, self.user_nw_ip, fail_limit_user, fail_limit_ip, lock_dur, account_lock=True)
                self.host_log(self.username, self.user_nw_ip, user_gw_ip, user_gw_cs, self.user_agent, '失败',
                              '密码错误')
                Log.logger.info(log_msg + ' "fail password_status"')
                # P0-5: 文案与"用户名不存在"统一为"账号或密码错误"，防用户名枚举
                return jsonify({'code': 100, 'msg': '账号或密码错误'})
        else:
            # 用户名不存在：仅增加 IP 失败计数（防枚举），不动账号维度
            # P0-5: 对齐 bcrypt 校验耗时，避免通过响应时间差枚举用户名
            dummy_verify_pwd(self.password)
            self._bump_login_fail(None, self.user_nw_ip, fail_limit_user, fail_limit_ip, lock_dur, account_lock=False)
            self.host_log(self.username, self.user_nw_ip, user_gw_ip, user_gw_cs, self.user_agent, '失败',
                          '用户名无效')
            Log.logger.info(log_msg + ' "fail user_status"')
            # P0-5: 文案与"密码错误"统一为"账号或密码错误"，防用户名枚举
            return jsonify({'code': 100, 'msg': '账号或密码错误'})

    def _bump_login_fail(self, username, ip, fail_limit_user, fail_limit_ip, lock_dur, account_lock=True):
        """累加登录失败计数；达到阈值时锁定账号 + IP。Redis 不可用时降级。
        P1-3: 账号维度用 fail_limit_user (默认 5)，IP 维度用 fail_limit_ip (默认 20)
              IP 阈值高 = 防公司 NAT 出口共享 IP 误锁全公司
        REV16 P1-2-LOW-1: 静默吞错 -> 至少 Log.logger.warning,
          Redis 不可用时退化为"无锁定"，增加被暴力破解风险 (虽然 bcrypt 慢哈希挡着)。
        """
        try:
            if ip:
                ip_key = f'login_fail_ip:{ip}'
                n = self.ords.conn.incr(ip_key)
                # P1-2: 失败计数窗口跟随锁定时长 (lock_dur)，不再硬编码 30 min
                self.ords.conn.expire(ip_key, lock_dur * 60)
                # P1-3: IP 维度用 fail_limit_ip
                if n >= fail_limit_ip:
                    self.ords.conn.set(f'login_lock_ip:{ip}', '1', ex=lock_dur * 60)
            if username and account_lock:
                u_key = f'login_fail:{username}'
                n = self.ords.conn.incr(u_key)
                # P1-2: 同上
                self.ords.conn.expire(u_key, lock_dur * 60)
                # P1-3: 账号维度用 fail_limit_user
                if n >= fail_limit_user:
                    self.ords.conn.set(f'login_lock:{username}', '1', ex=lock_dur * 60)
        except Exception as e:
            # REV16 P1-2-LOW-1: 不能静默, 至少 Log.logger.warning 记录原因 (告警运维/审计)
            Log.logger.warning(
                '_bump_login_fail Redis 不可用, 账号锁定降级为不锁定: '
                'username=%s ip=%s err=%s' % (username, ip, e)
            )


class UserRegister(UserLogin2):
    def __init__(self):
        super(UserRegister, self).__init__()
        self.email = request_param('email')
        self.verification = request_param('verification')
        self.ords = ConnRedis()

    def register(self):
        # 脱敏：日志不记录 password/verification 明文
        log_msg = 'req_body: [ username=%s, email=%s ] /account/com_register' % (
            self.username, self.email)
        # REV47-M6: 用户名查重只看 is_deleted=False (让软删 username 可复用)
        user_chk = t_acc_user.query.filter_by(name=self.username, is_deleted=False).first()
        # REV47-M6: 邮箱查重不过滤 (防恶意用同邮箱找回 / 重置密码攻击已软删账号)
        mail_chk = t_acc_user.query.filter_by(mail=self.email).first()
        if user_chk is None:
            if mail_chk is None:
                # P0-3: 与 CheckMail.send 保持一致, 使用 MAIL_VERIFY_PREFIX 读验证码
                verify_key = MAIL_VERIFY_PREFIX + self.email
                if not self.ords.conn.get(verify_key) is None:
                    # P1-4: 验证码以 SHA256 哈希存, 验证时同样哈希比对
                    if _hash_verify_code(self.verification) == self.ords.conn.get(verify_key):
                        # REVIEW-7-P0-1: 补 types='t_acc_user' 参数, 原 osql_in(alias=None, ...) 缺 types → tab_dict[None] KeyError
                        osql_in('t_acc_user', alias=None, name=self.username, password=hash_pwd(self.password),
                                usrole='user',
                                mail=self.email, group=None)
                        self.ords.conn.set(self.username + '_alias', 'user')
                        # P0-3: 验证成功后清验证码, 防重放
                        self.ords.conn.delete(verify_key)
                        return jsonify({'code': 0})
                    else:
                        Log.logger.info(log_msg + '"fail verification"')
                        return jsonify({'code': 100, 'msg': '验证码错误'})
                else:
                    Log.logger.info(log_msg + ' "fail chk_verification"')
                    return jsonify({'code': 100, 'msg': '验证码已过期'})
            else:
                Log.logger.info(log_msg + ' "fail chk_mail_status"')
                return jsonify({'code': 100, 'msg': '邮箱已被注册'})
        else:
            Log.logger.info(log_msg + ' "fail chk_user_status"')
            return jsonify({'code': 100, 'msg': '用户名已存在'})


class AccUserDel(CzToolsLog):
    def __init__(self):
        super(AccUserDel, self).__init__()
        _require_admin_or_raise()  # REV41-H1
        self.name = request_param('name')
        # 新增记录日志相关
        self.ords, self.cz_name = get_current_user()

    @property
    def host_del(self):
        log_msg = 'req_body: [ id=%s ] /account/user/del' % self.name
        # REV47-M6: soft_delete - 标记 is_deleted=True 而非物理删除
        user_chk = t_acc_user.query.filter_by(name=self.name, is_deleted=False).first()
        if user_chk:
            alias_name = user_chk.name
            user_chk.is_deleted = True
            db.session.commit()
            self.host_log(self.cz_name, '用户操作', '删除用户', self.name, '成功')
            self.ords.conn.delete(alias_name + '_alias')
            # REV16 P1-2-LOW-5: 同时清 _role Redis key, 避免删除后残留无主 key (Redis 内存累积)
            self.ords.conn.delete(alias_name + '_role')
            AuthAutoUpdate.user_grp_count(user_chk.group)
            return jsonify({'code': 0})
        else:
            self.host_log(self.cz_name, '用户操作', '删除用户', self.name, '失败', '系统内没有该用户')
            Log.logger.info(log_msg + ' \"fail acc_user_del_status\"')
            return jsonify({'code': 100, 'msg': '操作权限不足'})


# REV16 P1-2/MED-3: 仅在 self.password 非空时更新密码字段
#   拆分 update 字典, 空密码时不包含 'password' 字段, 其他字段保持不变。
# REV41-H2: 改名冲突检查逻辑提取为模块级函数, 便于单元测试


def _check_rename_conflict(up_user, new_name, lookup_fn):
    """REV41-H2: 检查将 up_user.name 改为 new_name 是否冲突.

    Args:
        up_user: 原 ORM 行 (需 .id 和 .name 属性), 若 None 表示 id 不存在
        new_name: 前端传入的新名
        lookup_fn(name) -> ORM 行 or None: 给定 name, 返回占用此名的行 (调用方负责 t_acc_user.query.filter_by)

    Returns:
        None 表示无冲突 (允许)
        str 表示冲突错误信息
    """
    if up_user is None:
        return '用户不存在'
    if up_user.name != new_name:
        conflict = lookup_fn(new_name)
        if conflict and conflict.id != up_user.id:
            return '该用户名已被占用'
    return None


class AccUserAdd(CzToolsLog):
    def __init__(self):
        super(AccUserAdd, self).__init__()
        _require_admin_or_raise()  # REV41-H1
        self.alias = request_param('alias')
        self.name = request_param('name')
        self.password = request_param('password')
        self.usrole = request_param('usrole')
        self.mail = request_param('mail')
        self.group = request_param('group')
        self.remarks = request_param('remarks', type=str, default=None)
        self.ords, self.cz_name = get_current_user()

    @property
    def host_add(self):
        # 脱敏：日志不记录 password 明文
        log_msg = 'req_body: [ alias=%s, name=%s, usrole=%s, mail=%s, group=%s, remarks=%s ] ' \
                  '/account/user/add' % (
                      self.alias, self.name, self.usrole, self.mail, self.group, self.remarks)
        try:
            user_chk = t_acc_user.query.filter_by(name=self.name).first()
            if user_chk is None:
                password_en = hash_pwd(self.password)
                db.session.add(t_acc_user(
                    alias=self.alias,
                    name=self.name,
                    password=password_en,
                    usrole=self.usrole,
                    mail=self.mail,
                    group=self.group,
                    remarks=self.remarks,
                ))
                sync_user_permissions(
                    None, self.name, False, self.usrole == 'admin',
                    commit=False,
                )
                db.session.commit()
                self.host_log(self.cz_name, '用户操作', '新增用户', self.name, '成功', None)
                self.ords.conn.set(self.name + '_alias', self.alias)
                self.ords.conn.set(self.name + '_role', self.usrole)
                AuthAutoUpdate.user_grp_count(self.group)
                return jsonify({'code': 0})
            else:
                self.host_log(self.cz_name, '用户操作', '新增用户', self.name, '失败', '该用户已存在')
                Log.logger.info(log_msg + ' \"fail sel_fail\"')
                return jsonify({'code': 100, 'msg': '操作权限不足'})
        except IOError:
            db.session.rollback()
            self.host_log(self.cz_name, '用户操作', '新增用户', self.name, '失败', '连接数据库错误')
            Log.logger.info(log_msg + ' \"fail con_fail\"')
            return jsonify({'code': 100, 'msg': '服务器内部错误'})
        except Exception:
            db.session.rollback()
            self.host_log(self.cz_name, '用户操作', '新增用户', self.name, '失败', '未知错误')
            Log.logger.info(log_msg + ' \"fail\"')
            return jsonify({'code': 100, 'msg': '操作失败 (code=2)'})


class AccUserUpdate(AccUserAdd):
    def __init__(self):
        super(AccUserUpdate, self).__init__()
        # AccUserAdd.__init__ 已做 admin 校验, 无需重复
        self.id = request_param('id')

    @property
    def update(self):
        # 脱敏：日志不记录 password 明文
        log_msg = 'req_body: [ id=%s, alias=%s, name=%s, usrole=%s, mail=%s, remarks=%s ] ' \
                  '/account/user/update' % (
                      self.id, self.alias, self.name, self.usrole, self.mail, self.remarks)
        try:
            # REV16 P1-2/MED-3: 仅在 self.password 非空时更新密码字段
            # 背景: 原实现无条件调用 hash_pwd + 写 password -> 管理员未传 password 时也会被覆盖为
            #       hash_pwd(None) = None, 账号瞬间被锁死, 用户必须走重置密码流程。
            #   修复: 拆分 update 字典, 空密码时不包含 'password' 字段, 其他字段保持不变。
            update_kwargs = {
                'alias': self.alias,
                'name': self.name,
                'usrole': self.usrole,
                'group': self.group,
                'mail': self.mail,
                'remarks': self.remarks,
            }
            if self.password:
                update_kwargs['password'] = hash_pwd(self.password)
            up_user = t_acc_user.query.filter_by(id=self.id).first()
            # REV41-H2: 改名校验 — 新 name 被他人占用时拒绝, id 不存在也直接拒绝
            rename_err = _check_rename_conflict(
                up_user,
                self.name,
                lambda n: t_acc_user.query.filter_by(name=n).first(),
            )
            if rename_err == '用户不存在':
                self.host_log(self.cz_name, '用户操作', '变更用户', self.name, '失败', '用户不存在')
                return jsonify({'code': 100, 'msg': '操作失败 (code=212)'})
            if rename_err is not None:
                self.host_log(self.cz_name, '用户操作', '变更用户', self.name, '失败', rename_err)
                Log.logger.info(log_msg + ' "fail name conflict"')
                return jsonify({'code': 100, 'msg': rename_err})
            old_name = up_user.name
            was_admin = up_user.usrole == 'admin'
            t_acc_user.query.filter_by(id=self.id).update(update_kwargs)
            sync_user_permissions(
                old_name,
                self.name,
                was_admin,
                self.usrole == 'admin',
                commit=False,
            )
            db.session.commit()
            role_name = self.name + '_role'
            self.ords.conn.set(role_name, self.usrole)
            self.host_log(self.cz_name, '用户操作', '变更用户', self.name, '成功')
            self.ords.conn.set(self.name + '_alias', self.alias)
            if old_name != self.name:
                self.ords.conn.delete(old_name + '_role')
                self.ords.conn.delete(old_name + '_alias')
            if up_user.group == self.group:
                AuthAutoUpdate.user_grp_count(self.group)
            else:
                AuthAutoUpdate.user_grp_count(up_user.group)
                AuthAutoUpdate.user_grp_count(self.group)
            return jsonify({'code': 0})
        except Exception:
            db.session.rollback()
            self.host_log(self.cz_name, '用户操作', '变更用户', self.name, '失败', '连接数据库错误')
            Log.logger.info(log_msg + ' \"fail\"')
            return jsonify({'code': 100, 'msg': '操作失败 (code=2)'})


class AccUserResetPwd(CzToolsLog):
    """管理员重置用户密码"""
    def __init__(self):
        super(AccUserResetPwd, self).__init__()
        _require_admin_or_raise()  # REV41-H1
        self.name = request_param('name')
        self.new_password = request_param('new_password')
        self.ords, self.cz_name = get_current_user()

    @property
    def reset_pwd(self):
        log_msg = 'req_body: [ name=%s ] /account/user/reset_pwd' % self.name
        try:
            user = t_acc_user.query.filter_by(name=self.name).first()
            if user:
                password_en = hash_pwd(self.new_password)
                user.password = password_en
                db.session.commit()
                self.host_log(self.cz_name, '用户操作', '重置用户密码', self.name, '成功')
                return jsonify({'code': 0})
            else:
                self.host_log(self.cz_name, '用户操作', '重置用户密码', self.name, '失败', '用户不存在')
                Log.logger.info(log_msg + ' "fail user not found"')
                return jsonify({'code': 100, 'msg': '用户名不存在'})
        except Exception:
            self.host_log(self.cz_name, '用户操作', '重置用户密码', self.name, '失败', '数据库错误')
            Log.logger.info(log_msg + ' "fail db error"')
            return jsonify({'code': 100, 'msg': '服务器内部错误'})


class ForgotPwdSend:
    """忘记密码 - 发送邮箱验证码"""
    def __init__(self):
        self.email = request_param('email')
        self.ords = ConnRedis()
        self.user_nw_ip = request.headers.get('X-Real-IP', '127.0.0.1')

    def send(self):
        log_msg = 'req_body: [ email=%s ] /account/forgot_pwd_send' % self.email
        mail_config = resolve_mail_configuration()
        if mail_config is None:
            return jsonify({'code': 100, 'msg': '管理员尚未配置邮件服务'})
        sendmail = build_mailer(mail_config)
        user = t_acc_user.query.filter_by(mail=self.email).first()
        if user:
            # CRIT-6 限流检查（重置密码验证码）
            allowed, retry_sec, err_code = _check_captcha_rate_limit(
                self.ords, self.email, self.user_nw_ip)
            if not allowed:
                return jsonify({'code': err_code, 'msg': f'请求过于频繁，请 {retry_sec} 秒后重试',
                                'retry_after': retry_sec})
            # P0-LOW-1: secrets 密码学安全随机数 (同 CheckMail.send)
            mail_verification = ''.join(secrets.choice(string.digits) for _ in range(6))
            msg = ("您在OrangeServer上重置密码的验证码是   %s   验证码有效期3分钟，请在3分钟内完成密码重置" % mail_verification)
            # REV16 P1-2/MED-2: 统一 MAIL_VERIFY_PREFIX 命名空间 + SHA256 哈希存储
            #   原实现: Redis key 用 `email + '_forgot'`, 与注册验证码不同命名空间, 且明文存储
            #   修复: 复用 MAIL_VERIFY_PREFIX (与 CheckMail.send 一致), SHA256 哈希存储
            #   意义: Redis dump 泄露不可逆读出明文验证码; 统一命名空间便于管理
            verify_key = MAIL_VERIFY_PREFIX + 'forgot:' + self.email
            self.ords.conn.set(verify_key, _hash_verify_code(mail_verification), ex=MAIL_VERIFY_TTL)
            sendmail.send(self.email, 'OrangeServer', '密码重置验证码', msg)
            return jsonify({'code': 0})
        else:
            # P0-6: 防反向枚举邮箱
            # 旧实现返 "邮箱已被注册" → 攻击者据此反向撞库（输入任意邮箱，若返此文案即存在）
            # 新实现：邮箱存在与不存在都返 code:0，仅在存在时真发邮件
            # 前端 [Login.vue:272](file:///d:/code/ogs198/pycharm_ogsfront/src/views/Login.vue#L272) 统一提示 "验证码已发送"
            # 日志内部仍记录“email not found”便于审计
            Log.logger.info(log_msg + ' "email not found, suppressed to client"')
            return jsonify({'code': 0})


class ForgotPwdReset:
    """忘记密码 - 验证码校验并重置密码"""
    def __init__(self):
        self.email = request_param('email')
        self.verification = request_param('verification')
        self.new_password = request_param('new_password')
        self.ords = ConnRedis()
        self.user_nw_ip = request.headers.get('X-Real-IP', '127.0.0.1')

    def _bump_forgot_fail(self):
        """P0-7: 累加忘记密码失败计数；达到阈值锁定邮箱 + IP 30 分钟。
        6 位纯数字 180s 过期 = 1000 组合，脚本可以穷举。失败 5 次锁 30 min 让攻击不经济。
        """
        try:
            if self.email:
                ek = f'forgot_fail:{self.email}'
                n = self.ords.conn.incr(ek)
                self.ords.conn.expire(ek, 30 * 60)
                if n >= 5:
                    self.ords.conn.set(f'forgot_lock:{self.email}', '1', ex=30 * 60)
            if self.user_nw_ip:
                ik = f'forgot_fail_ip:{self.user_nw_ip}'
                n = self.ords.conn.incr(ik)
                self.ords.conn.expire(ik, 30 * 60)
                if n >= 5:
                    self.ords.conn.set(f'forgot_lock_ip:{self.user_nw_ip}', '1', ex=30 * 60)
        except Exception:
            pass

    def reset(self):
        log_msg = 'req_body: [ email=%s ] /account/forgot_pwd_reset' % self.email
        try:
            # P0-7: 入口锁定检查（与登录锁定独立计数，5 次 / 30 min）
            if self.email and self.ords.conn.get(f'forgot_lock:{self.email}'):
                return jsonify({'code': 100, 'msg': '重置尝试过多，请 30 分钟后再试'})
            if self.user_nw_ip and self.ords.conn.get(f'forgot_lock_ip:{self.user_nw_ip}'):
                return jsonify({'code': 100, 'msg': '重置尝试过多，请 30 分钟后再试'})

            stored_code = self.ords.conn.get(MAIL_VERIFY_PREFIX + 'forgot:' + self.email)
            if stored_code is None:
                # P0-7: 验证码已过期也记为失败一次（防脚本探测）
                self._bump_forgot_fail()
                return jsonify({'code': 100, 'msg': '验证码已过期'})
            # REV16 P1-2/MED-2: SHA256 哈希比对（与存储方式一致）
            if _hash_verify_code(self.verification) != stored_code:
                # P0-7: 验证码错误必累加失败计数
                self._bump_forgot_fail()
                return jsonify({'code': 100, 'msg': '验证码错误'})
            user = t_acc_user.query.filter_by(mail=self.email).first()
            if not user:
                return jsonify({'code': 100, 'msg': '用户不存在'})

            # P0-7: 先删 Redis（防 commit 失败导致验证码可重用），再写 DB
            # 同时清零失败计数（验证成功意味着该轮的攻击未成功）
            self.ords.conn.delete(MAIL_VERIFY_PREFIX + 'forgot:' + self.email)
            if self.email:
                self.ords.conn.delete(f'forgot_fail:{self.email}')
            if self.user_nw_ip:
                self.ords.conn.delete(f'forgot_fail_ip:{self.user_nw_ip}')

            password_en = hash_pwd(self.new_password)
            user.password = password_en
            db.session.commit()
            return jsonify({'code': 0})
        except Exception:
            Log.logger.info(log_msg + ' "fail"')
            return jsonify({'code': 100, 'msg': '服务内部错误'})


class UserLogout:
    def __init__(self):
        self.ords = ConnRedis()
        self.user_token = request.cookies.get('ogs_token')

    def logout(self):
        # REVIEW-6-P2-4: 防止 cookie 丢失时 str(None) == 'None' 误查
        if not self.user_token or self.ords.conn.get(self.user_token) is None:
            return {'code': 100, 'msg': '未授权访问'}
        # 删 Redis 中的 token
        self.ords.conn.delete(self.user_token)
        # REVIEW-6-P1-3: 同步清理 csrf_nonce, 防 nonce 残留被重利用
        try:
            from app.tools.csrf import clear_csrf_nonce
            clear_csrf_nonce(self.user_token)
        except Exception:
            pass
        # CRIT-4：同步删客户端 cookie
        response = make_response(jsonify({'code': 0}))
        response.delete_cookie('ogs_token', path='/')
        # REVIEW-6-P1-3: 同步删 csrf_token cookie
        response.delete_cookie('csrf_token', path='/')
        return response
