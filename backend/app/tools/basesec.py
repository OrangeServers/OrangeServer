"""base security primitives for OrangeServer.

REV47-M13: 模块 docstring (REV46 P3 L13 / REV47-M13)

本模块集中所有密码学原语, 业务层统一从这里 import, 不直接调 bcrypt / Fernet.

核心能力:
  1) bcrypt 单向哈希 (t_acc_user.password):
     - hash_pwd(plain)         -> bcrypt hash str
     - verify_pwd(plain, hash) -> (matched, is_legacy_base64) tuple
     - needs_rehash(hash)      -> bool (用于登录后自动 rehash)
  2) Fernet 对称加密 (t_sys_user.host_password, SSH 凭据):
     - encrypt_host_password(plain)                       -> ciphertext str
     - decrypt_host_password(stored, rehash_callback=...) -> plain str
  3) 常时延 dummy 校验 (防用户名枚举):
     - dummy_verify_pwd(plain) -> None
  4) 密码算法版本号常量 (R2-6 / REV45-H9):
     - PWD_VERSION_LEGACY_BASE64 = 1
     - PWD_VERSION_BCRYPT_1 = 2 (当前默认)
     - PWD_VERSION_CURRENT = PWD_VERSION_BCRYPT_1
  5) 内置占位账号的不可登录口令:
     - UNUSABLE_PASSWORD_HASH (随机明文的 bcrypt 摘要, 明文不可恢复)

设计原则 (REV47-M12 / M14):
  - 全函数 type hints (M12): 入参 / 出参都标注, IDE / mypy 可校验
  - 命名统一 (M14): 入参 plain 表示明文, stored 表示已存储值 (hash / ciphertext)
  - 异常: 入参非法 (None / 空) 显式拒绝, 不静默 fallback 到不安全行为
  - 审计 (REV47-H8): 加解密走 _BASESEC_AUDIT_LOGGER namespace, 不污染业务 logger

环境变量:
  OGS_BCRYPT_ROUNDS    bcrypt rounds (默认 12, 范围 10-15)
  OGS_FERNET_KEYS      Fernet keys 逗号分隔, list[0] 用于加密
  OGS_FERNET_KEY       单 key 兼容写法
  OGS_DISABLE_BASE64_COMPAT  1 = 禁用 base64 兼容路径 (REV47-M15)
"""
import base64
import hmac
import logging
import re
import bcrypt
from cryptography.fernet import Fernet, InvalidToken
from typing import Callable, Optional, Tuple, Union

from app.core.config import _env


# REV47-M10: bcrypt cost 提取用 regex 替代 stored[4:6] 切片
#   背景: stored[4:6] 假设 cost 始终 2 位十进制, rounds=10..15 时 OK,
#         但 rounds >= 100 时 (2a$100$ 实际不存在, 但 rounds=12 之外的 future-proof)
#         bcrypt 格式: $2a$10$xxxxxx → '$2a$' + 2 位 cost + '$'
#   修复: 用 regex 严格匹配 $2[ayb]$XX$ 头, 提取 XX 部分
#   兼容: 老 rounds=10/12 仍正常, 但格式异常的 hash 不会错位切片
#   命名统一 (REV47-M14): 函数名 _extract_bcrypt_cost 清晰表达意图
_BCRYPT_COST_RE = re.compile(r'^\$2[abxy]\$(\d{2})\$')


def _extract_bcrypt_cost(stored: Optional[str]) -> Optional[int]:
    """REV47-M10: 从 bcrypt hash 提取 cost (regex 严格匹配).

    Args:
        stored: bcrypt hash 字符串 (e.g. '$2b$12$...')

    Returns:
        int: cost (e.g. 10/12), 格式异常返回 None
    """
    if not stored or not isinstance(stored, str):
        return None
    m = _BCRYPT_COST_RE.match(stored)
    if not m:
        return None
    try:
        return int(m.group(1))
    except (ValueError, TypeError):
        return None


# REV47-H8: encrypt/decrypt_host_password 审计 logger
#   命名空间: 'basesec_audit' (与 audlog_fallback 区分, 业务不混线)
#   目的: SSH 凭据加/解密是敏感操作, 失败/重加密需可追溯
#   - INFO: 成功 (op, key_index, stored_type)
#   - WARNING: 透明迁移 (旧 key 重加密 / 旧 base64 升级 Fernet)
#   - WARNING: 解密失败 (明文尝试, 但 OGS_FERNET_KEYS 都无法解)
_BASESEC_AUDIT_LOGGER = 'basesec_audit'


# REV47-M15: base64 兼容路径门控 (env-controlled)
#   背景: 历史 SSH 凭据用 base64 编码 (无加密), 已升级到 Fernet 后 base64 仍可解密
#         但 base64 数据 = 不安全, 长期保留是安全债
#   门控: OGS_DISABLE_BASE64_COMPAT=1 时, base64 数据直接抛 RuntimeError
#         强制要求业务侧先 rehash 升级到 Fernet, 才能解密
#   流程: 业务侧跑迁移脚本 (遍历 t_sys_user.host_password, base64 → Fernet)
#         验证全量升级后, 设 env=1 关闭兼容
#   默认: 0 (保持向后兼容, 已有 base64 数据仍能解)
# 动态读 env: 每次 decrypt 时查询 (避免模块级 import 时缓存, 允许运行时切换)


def _is_base64_compat_disabled():
    """REV47-M15: 查询 OGS_DISABLE_BASE64_COMPAT 门控 (动态读 env).

    返回 True 时, base64 兼容路径直接抛 RuntimeError.
    动态读 (非模块级缓存), 允许测试 monkeypatch + 运维侧运行时切换.
    """
    return _env('OGS_DISABLE_BASE64_COMPAT', '0') == '1'


# ============================================================================
# bcrypt 单向哈希：用于账号密码 (t_acc_user.password)
# ============================================================================
# bcrypt 适合"密码校验"场景：明文密码 + 存储的 hash → 比对 True/False。
# 不可逆，无法还原明文 → 适合登录密码，不适合需要解密的密钥。
_BCRYPT_PREFIXES = ('$2a$', '$2b$', '$2y$')
# REV46 P1-2/MED-6: bcrypt rounds 10 -> 12 (默认), 通过 OGS_BCRYPT_ROUNDS 环境变量可调
#   背景: 2024 OWASP 推荐 rounds >= 12 (~250ms/次); 10 rounds 已偏低 (业内 8-12 年前水平)。
#   修复: 默认 12, 可经 OGS_BCRYPT_ROUNDS env 调整 (生产建议 12, 性能敏感场景可降 10)。
#   升级路径: needs_rehash() 会检测旧 rounds 存锘, 登录成功后自动 rehash 为新 rounds。
_BCRYPT_ROUNDS = int(_env('OGS_BCRYPT_ROUNDS', '12'))
# 安全上下限: 低于 10 拒绝启动, 避免运维误调低
if _BCRYPT_ROUNDS < 10 or _BCRYPT_ROUNDS > 15:
    raise RuntimeError(
        f'OGS_BCRYPT_ROUNDS={_BCRYPT_ROUNDS} 超出安全范围 [10, 15]! '
        f'推荐 12 (OWASP 2024)。调整方法: 写入 backend/.env 或系统环境变量。'
    )


# =============================================================================
# R2-6 (REV45-H9): 密码算法版本号常量
# =============================================================================
# 用 Integer 列存到 t_acc_user.password_version, 业务层可查询/统计/审计:
#   - 多少账号还是 base64 旧格式 (VERSION_LEGACY_BASE64)
#   - 多少账号是 bcrypt (但 rounds 不同)
#   - 升级到 scrypt/argon2 时, 加 VERSION_SCRYPT_1 等
#
# 旧数据无 password_version 列时, 默认视为 VERSION_LEGACY_BASE64 (保守策略)
# need_rehash() 在登录成功后, 自动 rehash 并让调用方更新 password_version = VERSION_BCRYPT_1
PWD_VERSION_LEGACY_BASE64 = 1   # 旧: 纯 base64, 无哈希
PWD_VERSION_BCRYPT_1 = 2       # bcrypt, rounds ≥ 10
# 未来:
# PWD_VERSION_SCRYPT_1 = 3
# PWD_VERSION_ARGON2_1 = 4
PWD_VERSION_CURRENT = PWD_VERSION_BCRYPT_1


# =============================================================================
# 内置占位账号的不可登录口令
# =============================================================================
# 用于 system 这类"只为满足外键/默认值而存在, 永不交互式登录"的账号
# (t_cron.job_owner FK -> t_acc_user.name ON DELETE SET DEFAULT)。
#
# 为什么不能用 hash_pwd('!disabled-xxx!') 这类可读占位串:
#   占位串本身是入仓的公开明文, 任何人都能拿它登录 —— 等于把"禁用"变成了
#   "改用一个人人皆知的口令"。同理 base64 占位串会被 verify_pwd 的兼容路径
#   直接匹配成功。
# 这里存的是一次性随机明文的 bcrypt 摘要, 明文从未落盘且不可恢复, 因此任何输入
# 都无法通过 verify_pwd; 同时保留完整 bcrypt 开销, 不会因响应耗时差异暴露该账号
# 与真实账号的区别 (对齐 P0-5 的防枚举设计)。
#
# 该字面量同时被 backend/mysqldir/orange.sql 与 rev47_h7_cron_owner_fk.sql 播种,
# 三处必须一致, 由 tests/test_clean_deploy_contract.py 锁定。
UNUSABLE_PASSWORD_HASH = (
    '$2b$12$GrcI53JVdPLfQ/POoL9QBeuSC9lr2DgQ6MWaBHbGZWYSYRrcNiQ5K'
)


def base64_auto(base_type: str = 'de', string: Optional[str] = None) -> Optional[str]:
    """
    base_type-->传入en加密，传入de解密，str类型
    string-->传入字符串进行base64加密,str类型

    REV47-M12: 加 type hints. 命名与 encrypt_host_password / decrypt_host_password
    不一致 (无 plain/stored), 但属于低安全敏感度内部 helper, 不强制统一 (M14 部分项).
    """
    if base_type == 'en':
        msg = base64.b64encode(string.encode('utf-8'))
        return msg.decode()
    elif base_type == 'de':
        msg = base64.b64decode(string)
        return msg.decode()
    return None


def _is_bcrypt_hash(stored: Optional[str]) -> bool:
    """判断存储值是否为 bcrypt hash。bcrypt 长度固定 60 字符。

    REV47-M12: 加 type hints. 命名 stored 与 verify_pwd / needs_rehash 统一 (M14).
    """
    if not stored or not isinstance(stored, str):
        return False
    return stored.startswith(_BCRYPT_PREFIXES) and len(stored) == 60


def hash_pwd(plain: Optional[str]) -> Optional[str]:
    """对明文密码进行 bcrypt 哈希，返回 60 字符的 hash 字符串。

    用于 t_acc_user.password 的写入。

    REV46-H9: 拒绝空字符串密码 (防空密码账号), 但 None 仍返回 None (字段 NULL 语义).
    - None        -> None (字段保持 NULL, 业务层用其他方式管理)
    - '' 或 b''    -> raise ValueError (防御: 不允许空密码账号)
    - 正常密码     -> 60 字符 bcrypt hash

    业务层 (user.py) 多数路径已有 `if self.password:` 防御,
    basesec 这里是最后一道防线, 业务校验绕过时拦截.

    REV47-M12: 入参名 password -> plain (与 encrypt_host_password 统一, M14).
    """
    if plain is None:
        return None
    if isinstance(plain, str):
        plain = plain.encode('utf-8')
    # REV46-H9: bytes 空拒绝 (防 plain='' 绕过)
    if not plain:
        raise ValueError('password cannot be empty')
    return bcrypt.hashpw(plain, bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)).decode('utf-8')


def verify_pwd(plain: Optional[str], stored: str) -> Tuple[bool, bool]:
    """验证明文密码是否与存储值匹配。

    - 存储值为 bcrypt hash（$2a$/$2b$/$2y$ 开头）→ bcrypt 验证
    - 存储值为 base64（旧系统迁移兼容）→ base64 比较后返回 True
    - 其他情况 → False

    返回 (matched: bool, is_legacy_base64: bool)
    - matched: 密码是否正确
    - is_legacy_base64: 存储值是否还是旧的 base64 格式（True 时调用方应在登录成功后
                       自动调用 hash_pwd 重新 hash 写入，实现透明升级）

    REV47-M12: 加 type hints. REV47-M14: 入参 password -> plain (M14 统一).
    """
    if not plain or not stored:
        return False, False

    # 现代 bcrypt 路径
    if _is_bcrypt_hash(stored):
        pwd_bytes = plain.encode('utf-8') if isinstance(plain, str) else plain
        try:
            return bcrypt.checkpw(pwd_bytes, stored.encode('utf-8')), False
        except (ValueError, TypeError):
            return False, False

    # 旧 base64 兼容路径（仅用于过渡期）
    try:
        decoded = base64.b64decode(stored).decode('utf-8')
        # P1-10: 防时序攻击 — 字符串相等比较会随首个不同字符提前返回
        #   hmac.compare_digest 是常数时间比较，攻击者无法通过响应时间推断前缀匹配数
        matched = hmac.compare_digest(str(plain), str(decoded))
        return matched, matched  # 仅当匹配时标记为 legacy，下游触发 rehash
    except Exception:
        return False, False


def needs_rehash(stored: str) -> bool:
    """存储值是否需要被重新 hash。

    - 旧 base64 格式 → 需要 rehash（升级为 bcrypt）
    - bcrypt 但 rounds 与当前不匹配 → 需要 rehash
    - 当前 bcrypt + rounds 匹配 → 不需要

    REV47-M12: 加 type hints.
    """
    if not stored:
        return False
    if not _is_bcrypt_hash(stored):
        return True
    # REV47-M10: 用 regex 提取 cost, 替代老的 stored[4:6] 切片
    stored_rounds = _extract_bcrypt_cost(stored)
    if stored_rounds is None:
        return False
    return stored_rounds < _BCRYPT_ROUNDS


# ============================================================================
# P0-5: dummy bcrypt 校验（用于对齐登录响应耗时，防用户名枚举）
# ============================================================================
# 攻击者可观察"密码错误" vs "用户名不存在" 的响应时延差异来批量枚举有效账号。
# 用户名存在时走 verify_pwd → bcrypt.checkpw（~100ms @ rounds=10）
# 用户名不存在时本来 0ms，差异显著 → 暴露账号存在性。
# 解决：在"用户名不存在"分支也跑一次 dummy bcrypt.checkpw，对齐耗时。
# ============================================================================
_DUMMY_BCRYPT_HASH = bcrypt.hashpw(
    b'ogs_dummy_password_for_timing_equalize',
    bcrypt.gensalt(rounds=_BCRYPT_ROUNDS)
).decode('utf-8')


def dummy_verify_pwd(plain: Optional[str]) -> None:
    """对齐登录耗时的假 bcrypt 校验。

    调用方: [user.py:login_dl](file:///d:/code/OrangeServer/backend/app/users/user.py) 走完 `user_info is None` 分支前。
    - 不返回结果（目的只是消耗时间）
    - 接受 None/空串（不会抛异常）
    - 恒定时长（与 _BCRYPT_ROUNDS 严格挂钩，~100ms @ 10）

    REV46-M9: 修复 docstring 中错误路径引用 (d:/code/ogs198/pycharm_ogsbackend/... → 当前 OrangeServer 路径).
    REV47-M12: 加 type hints. REV47-M14: 入参 password -> plain (M14 统一).
    """
    if plain is None:
        plain = b''
    if isinstance(plain, str):
        plain = plain.encode('utf-8')
    try:
        bcrypt.checkpw(plain, _DUMMY_BCRYPT_HASH.encode('utf-8'))
    except Exception:
        # 任何异常吃掉，目的就是耗时对齐
        pass


# ============================================================================
# Fernet 对称加密：用于 SSH 凭据 (t_sys_user.host_password)
# ============================================================================
# 为什么不用 bcrypt？
#   bcrypt 是单向哈希，无法解密还原。SSH 客户端（paramiko）需要明文密码才能认证。
#   所以 SSH 凭据必须用对称加密。Fernet = AES-128-CBC + HMAC-SHA256，密钥 32 字节。
#
# 透明迁移策略：
#   1) 旧数据是 base64 编码（无加密，任何能拿到 DB 的人都能解）
#   2) 新数据是 Fernet 密文（无密钥解不开）
#   3) 读时自动判断：base64 → 解密 → 用当前 fernet key 重加密 → 写回 DB（一次性）
#
# 密钥管理：
#   OGS_FERNET_KEYS (推荐)  - 多个 key 逗号分隔, 第一个为最新 key (用于加密)
#   OGS_FERNET_KEY   (兼容)  - 单 key, 等价 OGS_FERNET_KEYS=<key>
#   缺失时 fail-fast, 不允许降级到不加密（避免静默回退到不安全状态）。
#
# REV46-H7: Fernet key rotation 支持
#   - 加密永远用最新 key (list[0])
#   - 解密按 list 顺序尝试所有 key (支持历史 key 解密已加密数据)
#   - 解密到非最新 key 加密的数据时, 自动用新 key 重加密并通过 callback 写回 DB
#   - 这样 key 泄露时: 部署时把新 key 加到 list[0], 旧 key 留在 list[1..],
#     业务运行中自动迁移, 全部数据用新 key 重加密后, 即可安全移除旧 key
# ============================================================================

# Fernet 密文特征：以 gAAAAA 开头（base64 编码的版本号 + 32 位时间戳）
_FERNET_PREFIX = 'gAAAAA'


def _get_fernet_list():
    """REV46-H7: 从 OGS_FERNET_KEYS (优先) 或 OGS_FERNET_KEY (兼容) 加载 key 列表.

    返回 list of Fernet instances, 按 list 顺序排序:
      - list[0] = 最新 key (用于加密)
      - list[1..] = 历史 key (仅用于解密)

    key 缺失时抛 RuntimeError (不静默回退).
    key 格式无效时抛 RuntimeError (指出具体哪个 key 出错).

    配置示例:
      OGS_FERNET_KEYS=new_key_xxx,old_key_yyy,older_key_zzz
      # new_key_xxx 用于加密, 所有 key 都可解密 (顺序无关)
      # rotation: 把新 key 放到 list[0], 旧 key 留在 list[1..]
    """
    # OGS_FERNET_KEYS 优先 (推荐配置), OGS_FERNET_KEY 兼容旧部署
    keys_str = _env('OGS_FERNET_KEYS', None) or _env('OGS_FERNET_KEY', '') or ''
    keys = [k.strip() for k in keys_str.split(',') if k.strip()]
    if not keys:
        raise RuntimeError(
            'OGS_FERNET_KEYS 未配置。SSH 凭据加密必须设置该环境变量。\n'
            '生成方式: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"\n'
            '多 key 格式 (rotation): OGS_FERNET_KEYS=new_key,old_key1,old_key2\n'
            '配置后写入 .env 或系统环境变量。'
        )
    result = []
    for idx, k in enumerate(keys):
        try:
            result.append(Fernet(k.encode('utf-8') if isinstance(k, str) else k))
        except Exception as e:
            raise RuntimeError(
                f'OGS_FERNET_KEYS 中第 {idx + 1} 个 key 格式无效 ({k[:10]}...): {e}'
            )
    return result


def _get_primary_fernet():
    """REV46-H7: 获取最新 key (list[0]) 用于加密.

    永远返回 _get_fernet_list()[0], 失败时 RuntimeError 透传.
    """
    return _get_fernet_list()[0]


def _get_fernet():
    """单 key 模式 (向后兼容包装).

    老调用方可能仍调用 _get_fernet(), 保持返回 Fernet 实例.
    等价 _get_primary_fernet() (新 key).
    """
    return _get_primary_fernet()


def _is_fernet_ciphertext(stored: Optional[str]) -> bool:
    """是否 Fernet 密文（以 gAAAAA 开头 = base64 编码的版本号 + 时间戳）

    REV47-M12: 加 type hints.
    """
    if not stored or not isinstance(stored, str):
        return False
    return stored.startswith(_FERNET_PREFIX)


def encrypt_secret(plain: Optional[Union[str, bytes]]) -> Optional[str]:
    """用当前 Fernet 主密钥加密通用服务端秘密.

    该接口用于 API Key 等新字段，不接受空字符串，也不兼容历史 base64，
    防止新秘密存储静默降级为可逆编码。
    """
    if plain is None:
        return None
    if not isinstance(plain, (str, bytes)):
        raise TypeError('secret must be str or bytes')
    if not plain:
        raise ValueError('secret cannot be empty')
    plain_bytes = plain.encode('utf-8') if isinstance(plain, str) else plain
    return _get_primary_fernet().encrypt(plain_bytes).decode('utf-8')


def decrypt_secret(stored: Optional[str]) -> Optional[str]:
    """使用配置的 Fernet key ring 解密通用服务端秘密.

    与 SSH 历史凭据接口不同，本接口只接受 Fernet 密文，绝不尝试把未知
    字符串按 base64 明文处理。
    """
    if stored is None:
        return None
    if not isinstance(stored, str) or not stored:
        raise ValueError('encrypted secret cannot be empty')
    if not _is_fernet_ciphertext(stored):
        raise RuntimeError('encrypted secret is not valid Fernet ciphertext')

    last_err = None
    for fernet in _get_fernet_list():
        try:
            return fernet.decrypt(stored.encode('utf-8')).decode('utf-8')
        except (InvalidToken, ValueError, UnicodeDecodeError) as exc:
            last_err = exc
    raise RuntimeError(
        'secret decrypt failed: configured Fernet keys cannot decrypt the value'
    ) from last_err


def encrypt_host_password(plain: Optional[str]) -> Optional[str]:
    """对 SSH 主机密码做对称加密，返回 Fernet 密文（str）。

    None/空 输入返回 None（保持字段为 NULL）。

    REV46-H7: 加密永远用最新 key (OGS_FERNET_KEYS list[0]).
    REV47-H8: 写 audit log (INFO), 包含 key_index, 失败时 WARNING.
    REV47-M12: 加 type hints.
    """
    if not plain:
        return None
    f = _get_primary_fernet()
    if isinstance(plain, str):
        plain = plain.encode('utf-8')
    log = logging.getLogger(_BASESEC_AUDIT_LOGGER)
    try:
        ciphertext = f.encrypt(plain).decode('utf-8')
        # INFO: 成功
        try:
            log.info('basesec encrypt ok: key_index=0, plain_len=%d', len(plain))
        except Exception:
            pass
        return ciphertext
    except Exception as e:
        # WARNING: 加密失败 (极罕见, 除非 fernet key 在中途失效)
        try:
            log.warning('basesec encrypt failed: err=%s', e)
        except Exception:
            pass
        raise


def decrypt_host_password(
    stored: Optional[str],
    rehash_callback: Optional[Callable[[str], None]] = None,
) -> Optional[str]:
    """解密 SSH 主机密码，自动兼容历史 base64 数据并触发透明迁移。

    参数:
        stored: 数据库里的字段值（可能是 base64 或 Fernet 密文）
        rehash_callback: 可选，调用方提供的回调，用于把 base64 数据升级为 Fernet
                         或把旧 key 加密的数据升级到新 key。
                         签名: rehash_callback(plain_text) -> 新 stored 值（写入 DB）
                         传 None 则只解密不升级。

    返回:
        明文密码（str），stored 为空时返回 None。

    异常:
        RuntimeError: Fernet 解密失败（所有 OGS_FERNET_KEYS 都无法解密或数据被篡改）

    REV46-H7: 尝试所有 key 解密, 找到能解密的 key 后:
      - 如果是最新 key (list[0]) 加密: 直接返回
      - 如果是历史 key (list[i], i>0) 加密: 自动用新 key 重加密并通过 callback 写回
        (透明迁移, 业务无感知)
    REV47-H8: 写 audit log:
      - INFO: decrypt ok (key_index, stored_type)
      - WARNING: 透明迁移 (rehash 触发, 旧 key → 新 key 或 base64 → Fernet)
      - WARNING: decrypt 失败 (所有 key 都无法解)
    """
    log = logging.getLogger(_BASESEC_AUDIT_LOGGER)
    if not stored:
        return None

    # Fernet 密文：尝试所有 key 解密
    if _is_fernet_ciphertext(stored):
        fernet_list = _get_fernet_list()
        last_err = None
        for i, f in enumerate(fernet_list):
            try:
                plain = f.decrypt(stored.encode('utf-8')).decode('utf-8')
                # 找到能解密的 key. 如果不是最新 key (i > 0), 自动迁移
                if i > 0 and rehash_callback is not None:
                    try:
                        new_stored = encrypt_host_password(plain)
                        if new_stored:
                            rehash_callback(new_stored)
                            # WARNING: 旧 key 解密 + 已 rehash
                            try:
                                log.warning(
                                    'basesec rehash ok: key_index=%d→0, stored_type=fernet',
                                    i,
                                )
                            except Exception:
                                pass
                        else:
                            # rehash 返回空 (encrypt 因 None 被旁路)
                            try:
                                log.warning(
                                    'basesec decrypt rehash skipped: key_index=%d, reason=encrypt_returned_none',
                                    i,
                                )
                            except Exception:
                                pass
                    except Exception as re:
                        # 迁移失败不影响解密使用, 下次再试
                        try:
                            log.warning(
                                'basesec rehash failed: key_index=%d, err=%s', i, re,
                            )
                        except Exception:
                            pass
                else:
                    # INFO: 最新 key 解密成功
                    try:
                        log.info(
                            'basesec decrypt ok: key_index=0, stored_type=fernet',
                        )
                    except Exception:
                        pass
                return plain
            except (InvalidToken, ValueError) as e:
                last_err = e
                continue
        # 所有 key 都失败
        try:
            log.warning(
                'basesec decrypt failed: key_count=%d, last_err=%s',
                len(fernet_list), last_err,
            )
        except Exception:
            pass
        raise RuntimeError(
            f'所有 Fernet key (N={len(fernet_list)}) 都无法解密数据: '
            f'最后错误: {last_err}. 可能原因: key 都失效 / 数据被篡改'
        )

    # 历史 base64 数据：解码出明文（向后兼容，与 Fernet key 无关）
    # REV47-M15: 当 OGS_DISABLE_BASE64_COMPAT=1 时, 强制 rehash 后才能解
    if _is_base64_compat_disabled():
        try:
            log.error(
                'basesec base64 compat disabled: stored_type=legacy_base64, '
                'data MUST be rehashed to Fernet first. '
                'rehash_callback=%s',
                'provided' if rehash_callback is not None else 'missing',
            )
        except Exception:
            pass
        raise RuntimeError(
            'base64 兼容路径已禁用 (OGS_DISABLE_BASE64_COMPAT=1). '
            '历史 base64 数据必须先 rehash 升级到 Fernet 才能解密. '
            '迁移方法: 用 decrypt_host_password(..., rehash_callback=...) '
            '遍历所有 host_password 字段升级, 完成后设置 env=1.'
        )
    try:
        plain = base64.b64decode(stored).decode('utf-8')
    except Exception as e:
        try:
            log.warning('basesec base64 decode failed: err=%s', e)
        except Exception:
            pass
        raise RuntimeError(f'host_password 历史 base64 解码失败：{e}')

    # INFO: base64 兼容读取成功
    try:
        log.info('basesec decrypt ok: stored_type=legacy_base64')
    except Exception:
        pass

    # 透明迁移：调用方提供 callback 时升级到 Fernet（用新 key 加密）
    if rehash_callback is not None:
        try:
            new_stored = encrypt_host_password(plain)
            if new_stored:
                rehash_callback(new_stored)
                # WARNING: 透明迁移完成 (base64 → Fernet)
                try:
                    log.warning(
                        'basesec rehash ok: stored_type=legacy_base64→fernet',
                    )
                except Exception:
                    pass
        except Exception as re:
            # 迁移失败不影响解密使用，下次再试
            try:
                log.warning(
                    'basesec rehash failed: stored_type=legacy_base64, err=%s', re,
                )
            except Exception:
                pass

    return plain
