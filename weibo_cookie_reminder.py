"""
=========================================================
微博 cookie 过期提醒模块（weibo_cookie_reminder.py）
=========================================================
职责：检查 RSSHub 里微博 cookie（SUB 登录凭证）的过期时间，
     快到期时（默认剩 ≤30 天）钉钉提醒——推给所有已配置群。

背景：
  微博源的采集 100% 依赖 RSSHub 的 WEIBO_COOKIES（由 CookieCloud
  从浏览器同步）。cookie 里 SUB 是真正的登录凭证，过期后微博源
  全部停更。本模块每天检查一次，提前 30 天提醒，避免同事不知道
  什么时候该重登，拖到断更才发现。

设计：
  - 解密逻辑与 RSSHub 的 cookiecloud/start.mjs 完全一致
    （CookieCloud legacy 算法：md5(uuid-password) 前16位做 passphrase，
     CryptoJS OpenSSL 格式 AES-256-CBC）
  - webhook 列表来自数据库 settings 表（dingtalk_webhooks），
    与聚合器其它推送一致——所有群都发
  - --test 模式只发「默认群」（验证通道用，不打扰其他群）
  - 防重复：同一到期时刻 6 天内只提醒一次（重登后到期时刻变了会重新提醒）

用法：
  python weibo_cookie_reminder.py            # 检查并推送（供定时任务调用）
  python weibo_cookie_reminder.py --test     # 发一条【测试】到默认群，验证通道
  python weibo_cookie_reminder.py --dry-run  # 只打印结论，不推送
=========================================================
"""

import base64
import hashlib
import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

import httpx
from Crypto.Cipher import AES
from Crypto.Util.Padding import unpad

from config import Config, PROJECT_ROOT
from notifier import DINGTALK_KEYWORD, _push_one
from settings import load_runtime_settings

logger = logging.getLogger("weibo_cookie_reminder")

# 关键：httpx 默认在 INFO 级把完整请求 URL 打进日志，而 CookieCloud 的
# /get/{UUID} 会带 UUID（也算密钥）。压制掉，避免密钥泄露到日志。
logging.getLogger("httpx").setLevel(logging.WARNING)

# 提醒阈值：SUB 剩多少天以内就提醒
REMIND_DAYS = 30
# 同一到期时刻的冷却期：6 天内不重复提醒
REMIND_COOLDOWN_DAYS = 6
# 防重复状态文件（放聚合器目录，随项目一起存放）
STATE_FILE = PROJECT_ROOT / ".weibo-cookie-notify.json"

# ---------- 读取 RSSHub 的 CookieCloud 配置 ----------

def _find_rsshub_env() -> str | None:
    """定位 RSSHub 的 .env（COOKIE_CLOUD_* 在这里）。"""
    candidates = [
        os.getenv("RSSHUB_ENV", "").strip() or None,
        # 默认约定：聚合器与 rsshub 两个目录同级
        #   <项目根>/ai-news-aggregator  →  <项目根>/rsshub
        PROJECT_ROOT.parent / "rsshub" / ".env",
    ]
    for c in candidates:
        if c and Path(c).exists():
            return str(c)
    return None


def _read_cookiecloud_cfg():
    """返回 (COOKIE_CLOUD_HOST, UUID, PASSWORD)，缺任一返回 (None,None,None)。"""
    env_path = _find_rsshub_env()
    if not env_path:
        logger.warning("找不到 RSSHub 的 .env（COOKIE_CLOUD_* 配置），跳过检查")
        return None, None, None
    cfg = {}
    for line in Path(env_path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip().strip('"').strip("'")
    host = (cfg.get("COOKIE_CLOUD_HOST") or "").rstrip("/")
    uuid = cfg.get("COOKIE_CLOUD_UUID") or ""
    pwd = cfg.get("COOKIE_CLOUD_PASSWORD") or ""
    if not (host and uuid and pwd):
        logger.warning("RSSHub .env 里缺 COOKIE_CLOUD_HOST/UUID/PASSWORD，跳过检查")
        return None, None, None
    return host, uuid, pwd


def _decrypt_cookiecloud(uuid: str, encrypted_b64: str, password: str) -> dict:
    """CookieCloud legacy 解密，与 RSSHub start.mjs 等价。"""
    passphrase = hashlib.md5(f"{uuid}-{password}".encode("utf-8")).hexdigest()[:16].encode("utf-8")
    raw = base64.b64decode(encrypted_b64)
    if raw[:8] != b"Salted__":
        raise ValueError("加密数据不是 CryptoJS OpenSSL 格式")
    salt, ct = raw[8:16], raw[16:]
    # EVP_BytesToKey(passphrase, salt, MD5) → 48 字节（key 32 + iv 16）
    d, prev = b"", b""
    while len(d) < 48:
        prev = hashlib.md5(prev + passphrase + salt).digest()
        d += prev
    key, iv = d[:32], d[32:48]
    plain = unpad(AES.new(key, AES.MODE_CBC, iv).decrypt(ct), AES.block_size)
    return json.loads(plain)


# ---------- 检查 SUB 过期 ----------

def get_sub_expiry():
    """
    从 CookieCloud 拉取并解密，找到 weibo.cn 的 SUB cookie。

    返回：
      (expiration_ts, days_left)：SUB 的过期时间戳和剩余天数；
      SUB 缺失/解密失败返回 (None, None)
    """
    host, uuid, pwd = _read_cookiecloud_cfg()
    if not host:
        return None, None
    try:
        resp = httpx.get(f"{host}/get/{uuid}", timeout=10)
        resp.raise_for_status()
        payload = resp.json()
        if not payload.get("encrypted"):
            logger.warning("CookieCloud 里没有加密数据（浏览器插件还没同步过？）")
            return None, None
        data = _decrypt_cookiecloud(uuid, payload["encrypted"], pwd)
    except Exception as e:
        logger.warning(f"拉取/解密 CookieCloud 失败：{e}")
        return None, None

    cookies = (data.get("cookie_data") or {}).get("weibo.cn") or []
    for c in cookies:
        if c.get("name") == "SUB" and c.get("expirationDate"):
            expiry = float(c["expirationDate"])
            days = int((expiry - time.time()) // 86400)
            return expiry, days
    logger.warning("weibo.cn 里没有 SUB cookie（浏览器登录过微博并同步了吗？）")
    return None, None


# ---------- 推送 ----------

# 钉钉必含关键词统一收敛在 notifier.DINGTALK_KEYWORD（顶部已 import 复用），
# 提醒消息标题和正文都要带上，否则拒收。


def _build_reminder_text(expiry_ts: float, days: int) -> str:
    dt = datetime.fromtimestamp(expiry_ts)
    return (
        f"## ⚠️ 微博 cookie 即将过期（{DINGTALK_KEYWORD}）\n\n"
        f"登录凭证 **SUB** 将在 **{dt:%Y-%m-%d %H:%M}** 过期（剩约 **{days} 天**）。\n\n"
        f"**请尽快处理：**\n"
        f"1. 浏览器打开 https://weibo.com 重新登录一次\n"
        f"2. 点 CookieCloud 插件「同步」\n"
        f"3. 30 秒内 RSSHub 自动热更新，无需重启任何服务\n\n"
        f"若不处理，宇树/智元/优必选等 **7 个微博源将停更**。"
    )


def _load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _should_notify(expiry_ts: float, state: dict) -> bool:
    """同一到期时刻 6 天内只提醒一次；到期时刻变化（重登过）→ 重新提醒。"""
    if state.get("lastExpiry") != expiry_ts:
        return True
    try:
        last = datetime.fromisoformat(state["lastPushed"]).timestamp()
    except Exception:
        return True
    return (time.time() - last) >= REMIND_COOLDOWN_DAYS * 86400


def check_and_notify(test_only: bool = False, dry_run: bool = False) -> dict:
    """
    主入口：检查 SUB 过期时间并按需推送。

    参数：
      test_only: True → 只发一条【测试】到「默认群」，验证通道
      dry_run:   True → 只打印结论，不推送

    返回：
      结果 dict（供日志/CLI 展示），形如
      {"ok": True, "sub_expiry": "2027-08-05 14:20", "days_left": 360, "pushed": []}
    """
    # 从数据库刷新 webhook 列表（含所有群），与聚合器其它推送一致
    load_runtime_settings()
    webhooks = getattr(Config, "DINGTALK_WEBHOOKS", None) or []

    expiry, days = get_sub_expiry()
    if expiry is None:
        return {"ok": False, "reason": "SUB cookie 读取失败，跳过推送"}

    dt = datetime.fromtimestamp(expiry)
    result = {"ok": True, "sub_expiry": dt.strftime("%Y-%m-%d %H:%M"), "days_left": days, "pushed": []}

    if not webhooks:
        logger.warning("数据库里没有任何钉钉群 webhook，跳过推送")
        result["ok"] = False
        result["reason"] = "未配置钉钉群 webhook"
        return result

    if dry_run:
        return result  # 只检查不推送

    # ---- --test：只发默认群 ----
    if test_only:
        default = next((w for w in webhooks if w.get("name") == "默认群"), None)
        if not default:
            result["ok"] = False
            result["reason"] = "数据库里没有「默认群」webhook"
            return result
        body = {
            "msgtype": "markdown",
            "markdown": {
                "title": f"【测试】微博 cookie 提醒通道验证（{DINGTALK_KEYWORD}）",
                "text": (
                    f"## ✅ 提醒通道测试（{DINGTALK_KEYWORD}）\n\n"
                    f"这条由 `weibo_cookie_reminder.py --test` 发出，确认提醒能送达 **默认群**。\n\n"
                    f"当前 SUB 剩余 **{days} 天**。正式提醒会在剩 ≤{REMIND_DAYS} 天时推给**所有群**。"
                ),
            },
        }
        logger.info("发送【测试】消息到「默认群」...")
        ok = _push_one(default["url"], body)
        result["pushed"] = [{"name": default["name"], "ok": ok}]
        result["ok"] = ok
        return result

    # ---- 正式：剩 ≤30 天才推，推给所有群 ----
    if days > REMIND_DAYS:
        result["reason"] = f"剩余 {days} 天 > {REMIND_DAYS}，无需提醒"
        return result

    if not _should_notify(expiry, _load_state()):
        result["reason"] = "该到期时刻 6 天内已提醒过，跳过"
        return result

    body = {
        "msgtype": "markdown",
        "markdown": {"title": "微博 cookie 即将过期提醒", "text": _build_reminder_text(expiry, days)},
    }
    result["pushed"] = []
    all_ok = True
    for w in webhooks:
        ok = _push_one(w["url"], body)
        result["pushed"].append({"name": w["name"], "ok": ok})
        all_ok = all_ok and ok
        logger.info(f"推送 → {w.get('name')}：{'成功' if ok else '失败'}")

    if all_ok:
        _save_state({"lastExpiry": expiry, "lastPushed": datetime.now().isoformat()})
        result["reason"] = f"已推送到 {len(webhooks)} 个群（同一到期时刻 {REMIND_COOLDOWN_DAYS} 天内不重复）"
    else:
        result["reason"] = "部分群推送失败，不记录冷却（下轮重试）"
    return result


# ---------- CLI 入口 ----------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    test_only = "--test" in sys.argv
    dry_run = "--dry-run" in sys.argv
    r = check_and_notify(test_only=test_only, dry_run=dry_run)
    print(json.dumps(r, ensure_ascii=False, indent=2))
