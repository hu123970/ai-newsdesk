"""
=========================================================
认证模块（auth.py）
=========================================================
职责：全站登录（单账号）、密码哈希、会话 cookie、密码找回/修改。
=========================================================
设计：
  - 单账号：用户名固定（AUTH_USERNAME），密码哈希存 settings 表
  - 密码哈希：stdlib hashlib.pbkdf2_hmac（免 bcrypt 依赖）
  - 会话：自写 HMAC-SHA256 签名 cookie
      token = base64(payload).sig
      payload = "过期时间戳:密码哈希前缀"
    密码哈希前缀用来在改密码后作废所有旧会话（改密即全体登出）。
  - 找回：生成 6 位重置码 → 钉钉推送【所有】群（标题含关键词）→ 校验后重置密码
  - 防暴力：重置码 5 次尝试作废 + 60s 冷却 + 校验失败 sleep 1s

用法（main.py 里）：
  @app.middleware("http")
  async def auth_guard(request, call_next):
      return await auth.auth_middleware(request, call_next)
=========================================================
"""

import base64
import hashlib
import hmac
import json
import logging
import secrets
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import Config
from database import get_settings, set_setting
from notifier import DINGTALK_KEYWORD, push_report_markdown

logger = logging.getLogger("auth")


def _get_setting(key: str) -> str:
    """读 settings 表单项（database.get_settings 返回整表 dict）。"""
    return (get_settings() or {}).get(key, "") or ""

# ---------- 常量 ----------
AUTH_USERNAME = "ai-news"         # 管理员用户名（唯一）
USER_USERNAME = "staff"           # 普通用户用户名（唯一，独立于管理员）
DEFAULT_PASSWORD = "12345678"     # 管理员首次启动的默认密码
DEFAULT_USER_PASSWORD = "12345678"  # 用户首次启动的默认密码
SESSION_COOKIE = "session_token"  # cookie 名
AUTH_SESSION_DAYS = 7             # 会话有效期（天）
RESET_TTL_MINUTES = 10            # 重置码有效期
RESET_MAX_ATTEMPTS = 5            # 重置码最多尝试次数
RESET_COOLDOWN_SECONDS = 60       # 重新申请重置码的最小间隔

# settings 表里用到的键
SETTING_PASSWORD = "auth_password_hash"   # 管理员密码哈希（pbkdf2$...）
SETTING_USER_PASSWORD = "user_password_hash"  # 用户密码哈希（独立键，改密码互不影响）
SETTING_SECRET = "session_secret"         # 会话签名密钥（启动自动生成）
SETTING_RESET = "reset_code"              # 重置码状态（JSON）

_PBKDF2_ITERATIONS = 200_000


# ---------- 密码哈希 ----------

def hash_password(password: str) -> str:
    """pbkdf2 哈希，格式 pbkdf2$iter$salt_hex$hash_hex。"""
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS
    )
    return f"pbkdf2${_PBKDF2_ITERATIONS}${salt}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码（恒定时间比较）。任何格式异常都返回 False，不抛错。"""
    try:
        algo, iterations, salt, expected = stored.split("$")
        if algo != "pbkdf2":
            return False
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(digest.hex(), expected)
    except Exception:
        return False


# ---------- 会话密钥 ----------

def _session_secret() -> bytes:
    """会话签名密钥：优先 .env 的 SESSION_SECRET，否则取 settings 表存储的。"""
    env = (getattr(Config, "SESSION_SECRET", "") or "").strip()
    if env:
        return env.encode("utf-8")
    stored = _get_setting(SETTING_SECRET) or ""
    if not stored:
        stored = secrets.token_hex(32)
        set_setting(SETTING_SECRET, stored)
        logger.warning("SESSION_SECRET 未配置，已随机生成并存入数据库（重启不失效）；部署前建议写死到 .env")
    return stored.encode("utf-8")


def _pw_prefix(role: str) -> str:
    """该角色密码哈希的前 8 位（sha256 摘要），改该身份密码后其旧会话自动作废。

    角色分离：admin 用管理员密码哈希前缀，user 用用户密码哈希前缀——
    管理员改自己的密码不会把用户踢下线，反之亦然。
    """
    key = SETTING_USER_PASSWORD if role == "user" else SETTING_PASSWORD
    h = _get_setting(key) or ""
    return hashlib.sha256(h.encode("utf-8")).hexdigest()[:8]


# ---------- 会话 token ----------

def make_session_token(role: str = "admin") -> str:
    """生成签名会话 token（7 天有效），payload 含角色 + 对应密码前缀。"""
    expiry = int(time.time()) + AUTH_SESSION_DAYS * 86400
    payload = f"{expiry}:{role}:{_pw_prefix(role)}"
    sig = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
    b64 = base64.urlsafe_b64encode(payload.encode("utf-8")).decode("ascii").rstrip("=")
    return f"{b64}.{sig}"


def verify_session_token(token: str) -> Optional[str]:
    """
    校验会话 token：签名正确 + 未过期 + 密码前缀一致（改密即失效）。

    返回：
      角色（'admin' / 'user'），无效返回 None。

    注意：旧格式 token（不带角色字段）校验会失败 → 升级后全员重新登录一次，可接受。
    """
    try:
        b64, _, sig = token.partition(".")
        payload = base64.urlsafe_b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
        expiry, _, rest = payload.partition(":")
        role, _, prefix = rest.partition(":")
        if int(expiry) < time.time():
            return None
        if role not in ("admin", "user"):
            return None
        expected = hmac.new(_session_secret(), payload.encode("utf-8"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, sig):
            return None
        if not hmac.compare_digest(prefix, _pw_prefix(role)):
            return None
        return role
    except Exception:
        return None


# ---------- 登录 ----------

def verify_login(username: str, password: str) -> Optional[str]:
    """
    校验用户名密码，返回角色（'admin' / 'user'）或 None。

    双身份（2026-08-17）：管理员账号 ai-news → 'admin'；
    用户账号 staff → 'user'。各自用独立的密码哈希键，互不影响。
    """
    username = (username or "").strip()
    if username == AUTH_USERNAME:
        stored = _get_setting(SETTING_PASSWORD) or ""
        if stored and verify_password(password, stored):
            return "admin"
        return None
    if username == USER_USERNAME:
        stored = _get_setting(SETTING_USER_PASSWORD) or ""
        if stored and verify_password(password, stored):
            return "user"
        return None
    return None


def ensure_default_password() -> None:
    """
    首次启动：settings 表里还没有密码哈希时，写入默认密码 12345678。
    现有 settings 种子分支因表已有数据不会执行，必须单独调用。
    """
    if not _get_setting(SETTING_PASSWORD):
        set_setting(SETTING_PASSWORD, hash_password(DEFAULT_PASSWORD))
        logger.warning("已初始化默认管理员账号 %s / %s，请尽快登录后在设置中修改密码",
                       AUTH_USERNAME, DEFAULT_PASSWORD)


def ensure_default_user_password() -> None:
    """
    首次启动：settings 表里还没有用户密码哈希时，写入用户默认密码 12345678。
    与管理员密码独立存储，管理员改密码不影响用户，反之亦然。
    """
    if not _get_setting(SETTING_USER_PASSWORD):
        set_setting(SETTING_USER_PASSWORD, hash_password(DEFAULT_USER_PASSWORD))
        logger.warning("已初始化默认用户账号 %s / %s，管理员可在设置中修改", USER_USERNAME, DEFAULT_USER_PASSWORD)


def set_user_password(new_password: str) -> tuple[bool, str]:
    """管理员修改用户（staff）的密码；成功后用户身份的旧会话全部作废。"""
    new_password = (new_password or "").strip()
    if len(new_password) < 6:
        return False, "新密码至少 6 位"
    set_setting(SETTING_USER_PASSWORD, hash_password(new_password))
    return True, "用户密码已修改"


# ---------- 密码找回（钉钉推送重置码到所有群） ----------

def _load_reset_state() -> dict:
    raw = _get_setting(SETTING_RESET) or ""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def request_password_reset() -> tuple[bool, str]:
    """
    生成 6 位重置码并推送到【所有】钉钉群。
    返回 (ok, msg)。
    """
    from settings import load_runtime_settings
    load_runtime_settings()
    webhooks = getattr(Config, "DINGTALK_WEBHOOKS", None) or []
    if not webhooks:
        return False, "未配置钉钉群 webhook，无法推送重置码"

    now = datetime.now(timezone.utc)
    state = _load_reset_state()
    if state.get("last_request"):
        try:
            last = datetime.fromisoformat(state["last_request"])
            if (now - last).total_seconds() < RESET_COOLDOWN_SECONDS:
                wait = RESET_COOLDOWN_SECONDS - int((now - last).total_seconds())
                return False, f"请求太频繁，请 {wait} 秒后再试"
        except Exception:
            pass

    code = f"{secrets.randbelow(1_000_000):06d}"
    new_state = {
        "code_sha": hashlib.sha256(code.encode("utf-8")).hexdigest(),
        "expires_at": (now + timedelta(minutes=RESET_TTL_MINUTES)).isoformat(),
        "attempts": 0,
        "last_request": now.isoformat(),
    }
    set_setting(SETTING_RESET, json.dumps(new_state, ensure_ascii=False))

    # 推送所有群（正式提醒/日报同款规则，复用 notifier 的群发助手）
    cn = now.astimezone(timezone(timedelta(hours=8)))
    text = (
        f"## 🔑 登录密码重置码（{DINGTALK_KEYWORD}）\n\n"
        f"有人在 **{cn:%Y-%m-%d %H:%M}** 请求重置「AI 行业动态聚合」的登录密码。\n\n"
        f"重置码：**{code}**\n\n"
        f"请在 **{RESET_TTL_MINUTES} 分钟内**在登录页输入该码设置新密码。\n\n"
        f"若非本人操作，请忽略本条消息。"
    )
    ok = push_report_markdown(text, title=f"🔑 登录密码重置码（{DINGTALK_KEYWORD}）")
    if ok:
        return True, "重置码已推送到钉钉群，请在 10 分钟内使用"
    return False, "重置码已生成，但推送失败，请稍后重试"


def reset_password(code: str, new_password: str) -> tuple[bool, str]:
    """
    校验重置码并重置密码。
    返回 (ok, msg)。成功后作废重置码并让所有旧会话失效。
    """
    code = (code or "").strip()
    new_password = (new_password or "").strip()
    if len(new_password) < 6:
        return False, "新密码至少 6 位"

    state = _load_reset_state()
    if not state:
        return False, "没有待处理的重置请求，请先申请重置码"
    try:
        expires = datetime.fromisoformat(state["expires_at"])
        if datetime.now(timezone.utc) > expires:
            return False, "重置码已过期，请重新申请"
    except Exception:
        return False, "重置请求无效，请重新申请"

    if state.get("attempts", 0) >= RESET_MAX_ATTEMPTS:
        return False, "尝试次数过多，请重新申请重置码"

    time.sleep(1)  # 拖慢暴力尝试
    expected = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if not hmac.compare_digest(expected, state["code_sha"]):
        state["attempts"] = int(state.get("attempts", 0)) + 1
        set_setting(SETTING_RESET, json.dumps(state, ensure_ascii=False))
        return False, "重置码错误"

    set_setting(SETTING_PASSWORD, hash_password(new_password))
    set_setting(SETTING_RESET, "")
    return True, "密码已重置，请用新密码登录"


def change_password(old_password: str, new_password: str) -> tuple[bool, str]:
    """修改密码：验证旧密码 → 更新哈希（同时作废所有旧会话）。"""
    new_password = (new_password or "").strip()
    if len(new_password) < 6:
        return False, "新密码至少 6 位"
    stored = _get_setting(SETTING_PASSWORD) or ""
    if not stored or not verify_password(old_password or "", stored):
        return False, "旧密码错误"
    set_setting(SETTING_PASSWORD, hash_password(new_password))
    return True, "密码已修改"


# ---------- HTTP 守卫（FastAPI 中间件） ----------

# 无需登录即可访问的路径
_PUBLIC_PREFIXES = ("/static",)
_PUBLIC_PATHS = {"/login", "/favicon.ico", "/api/auth/request_reset", "/api/auth/reset"}


async def auth_middleware(request, call_next):
    """
    FastAPI HTTP 中间件：全站单点登录守卫。
    未登录时按请求类型分派：
      - HTMX 请求  → 401 + 头 HX-Redirect: /login（前端自动跳转）
      - /api/*     → 401 JSON（设置弹窗等 fetch 调用能识别）
      - 普通页面   → 302 /login?next=<原地址>（登录后跳回）
    """
    path = request.url.path
    if path.startswith(_PUBLIC_PREFIXES) or path in _PUBLIC_PATHS:
        return await call_next(request)

    token = request.cookies.get(SESSION_COOKIE, "")
    role = verify_session_token(token) if token else None
    if role:
        # 把身份塞进 request.state，路由里用它做权限判断 / 页面显示当前用户
        request.state.role = role
        request.state.username = AUTH_USERNAME if role == "admin" else USER_USERNAME
        return await call_next(request)

    from fastapi.responses import JSONResponse, RedirectResponse
    from urllib.parse import quote

    if request.headers.get("HX-Request") == "true":
        return JSONResponse(
            {"ok": False, "msg": "未登录"},
            status_code=401,
            headers={"HX-Redirect": "/login"},
        )
    if path.startswith("/api/"):
        return JSONResponse({"ok": False, "msg": "未登录或会话已过期"}, status_code=401)
    next_url = quote(request.url.path + (f"?{request.url.query}" if request.url.query else ""), safe="")
    return RedirectResponse(url=f"/login?next={next_url}", status_code=302)
