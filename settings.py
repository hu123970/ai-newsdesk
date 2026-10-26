"""
=========================================================
运行时配置层（settings.py）
=========================================================
职责：让 DeepSeek API Key、钉钉 webhook、监控公司列表
      可以在前端设置弹窗中运行时修改，立即生效、无需重启。
=========================================================
学习注释：
  为什么要有这一层？
  - Config 的类属性在 import 时从 .env 读取后就冻结了，
    直接改 .env 不重启无效。
  - 但读取点都在"调用时"（llm_processor 每次跑读 key、
    notifier 每次推送读 webhook、prompts 每次重建提示词），
    所以只要保存时给 Config 属性赋值 / 重建 prompts 常量，
    下一次调用就自动吃到新值——这就是"热更新"。

  三通道热更新：
  1. 写数据库 settings 表（持久化，重启不丢）
  2. 同步 Config 类属性（LLM key / webhook 列表）
  3. 重建 prompts 常量（监控公司 → 预筛选 + LLM 提示词）
=========================================================
"""

import json
import logging
import os

from config import Config
from database import get_settings, set_setting
from providers import PROVIDERS, DEFAULT_PROVIDER
import prompts

# ---------- 日志 ----------
logger = logging.getLogger("settings")

# ---------- 校验常量 ----------
_VALID_REGIONS = ("china", "overseas")
_VALID_CATEGORIES = ("ai_model", "data_labeling", "embodied_ai")
_DINGTALK_URL_PREFIX = "https://oapi.dingtalk.com/"


# ---------- 打码工具 ----------

def mask_key(key: str) -> str:
    """API Key 打码：sk-1234****6789。空值返回空串。"""
    if not key:
        return ""
    if len(key) <= 8:
        return key[:2] + "****"
    return key[:3] + "****" + key[-4:]


def mask_webhook(url: str) -> str:
    """Webhook URL 打码：隐藏 access_token 的值，保留域名和参数名。"""
    if not url:
        return ""
    if "access_token=" in url:
        head, _, token = url.partition("access_token=")
        token_masked = (token[:4] + "****") if len(token) > 4 else "****"
        return head + "access_token=" + token_masked
    return url


def _parse_json_list(raw: str) -> list:
    """解析 settings 表里的 JSON 列表（webhook / 公司），脏数据返回空列表。"""
    try:
        items = json.loads(raw or "[]")
        return [i for i in items if isinstance(i, dict)]
    except (json.JSONDecodeError, TypeError):
        return []


# ---------- 供应商配置解析 ----------

def _parse_providers(raw: str) -> dict:
    """
    解析 settings 表里的供应商配置 JSON dict {provider: {api_key, base_url, model}}。
    只保留已知供应商，字段缺失/空值用 PROVIDERS 默认值兜底；脏数据返回空 dict。
    """
    try:
        data = json.loads(raw or "{}")
        if not isinstance(data, dict):
            return {}
    except (json.JSONDecodeError, TypeError):
        return {}
    out = {}
    for pid, cfg in data.items():
        if pid not in PROVIDERS or not isinstance(cfg, dict):
            continue
        default = PROVIDERS[pid]
        out[pid] = {
            "api_key": str(cfg.get("api_key") or "").strip(),
            "base_url": str(cfg.get("base_url") or "").strip() or default["base_url"],
            "model": str(cfg.get("model") or "").strip() or default["model"],
        }
    return out


def _current_provider_cfg(provider: str) -> dict:
    """取指定供应商的当前配置（缺省用默认值），并回写默认 base_url/model。"""
    providers = _parse_providers((get_settings() or {}).get("llm_providers") or "")
    cfg = providers.get(provider) or {}
    default = PROVIDERS.get(provider) or PROVIDERS[DEFAULT_PROVIDER]
    cfg.setdefault("api_key", "")
    cfg.setdefault("base_url", default["base_url"])
    cfg.setdefault("model", default["model"])
    return cfg


# ---------- 启动加载 ----------

def load_runtime_settings():
    """
    启动时调用：用数据库 settings 表初始化运行时状态。

    行为：
      1. 表为空 → 用 .env 值写种子（首次启动）
      2. 供应商配置迁移：老库只有 deepseek_api_key → 灌进 llm_providers["deepseek"]
      3. 把当前供应商的 api_key/base_url/model 同步到
         Config.DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL / DEEPSEEK_MODEL
      4. 同步 Config.DINGTALK_WEBHOOKS、调 prompts.reload_prompts() 重建提示词常量

    学习注释：
      多供应商改造（2026-08-17）：所有 LLM 调用点仍读 Config.DEEPSEEK_*，
      这里在启动时把"当前供应商"的配置同步过去，调用点零改动。
      种子逻辑保证：老用户升级后，.env 里的 key 自动变成第一份供应商配置，
      行为与之前完全一致。
    """
    db = get_settings()

    if not db:
        # ---- 首次启动：写种子 ----
        default_webhooks = []
        env_url = (Config.DINGTALK_WEBHOOK_URL or "").strip()
        if env_url:
            default_webhooks = [{"name": "默认群", "url": env_url}]

        set_setting("dingtalk_webhooks", json.dumps(default_webhooks, ensure_ascii=False))
        set_setting("target_companies", json.dumps(prompts.DEFAULT_COMPANIES, ensure_ascii=False))
        db = get_settings()

    # ---- 当前使用的供应商（环境变量优先，便于部署时用配置固定） ----
    # 优先级：LLM_ACTIVE_PROVIDER（环境变量）> 数据库 > 默认 deepseek。
    # 这样容器重启、数据库回滚之后，部署配置依然能决定用哪家。
    env_active = (os.getenv("LLM_ACTIVE_PROVIDER") or "").strip()
    active = env_active or (db.get("llm_active_provider") or "").strip() or DEFAULT_PROVIDER
    if active not in PROVIDERS:
        active = DEFAULT_PROVIDER

    # ---- 供应商配置迁移（老库兼容） ----
    # 老版本只有 deepseek_api_key 键；升级后把它灌进 llm_providers["deepseek"]，
    # 保证已配置的 DeepSeek Key 不丢，且后续走统一的多供应商读写。
    providers = _parse_providers(db.get("llm_providers") or "")
    if not providers:
        old_key = (db.get("deepseek_api_key") or "").strip() or (Config.DEEPSEEK_API_KEY or "").strip()
        if old_key:
            providers = {"deepseek": {
                "api_key": old_key,
                "base_url": PROVIDERS["deepseek"]["base_url"],
                "model": PROVIDERS["deepseek"]["model"],
            }}
            set_setting("llm_providers", json.dumps(providers, ensure_ascii=False))

    # ---- 当前供应商还没有 Key 时，用环境变量补种 ----
    # 部署场景（容器盘是临时的）靠这段保证：每次启动都能从环境变量把 Key 恢复回来。
    # 通用变量：LLM_API_KEY / LLM_BASE_URL / LLM_MODEL（对任何供应商都适用）；
    # 兼容变量：DEEPSEEK_*（仅当前供应商是 deepseek 时兜底，老配置不用改）。
    cur_cfg = providers.get(active) or {}
    if not (cur_cfg.get("api_key") or "").strip():
        env_key = (os.getenv("LLM_API_KEY") or "").strip()
        env_base = (os.getenv("LLM_BASE_URL") or "").strip()
        env_model = (os.getenv("LLM_MODEL") or "").strip()
        if active == "deepseek":
            env_key = env_key or (os.getenv("DEEPSEEK_API_KEY") or "").strip()
            env_base = env_base or (os.getenv("DEEPSEEK_BASE_URL") or "").strip()
            env_model = env_model or (os.getenv("DEEPSEEK_MODEL") or "").strip()
        if env_key:
            default = PROVIDERS.get(active) or PROVIDERS[DEFAULT_PROVIDER]
            providers[active] = {
                "api_key": env_key,
                "base_url": env_base or default["base_url"],
                "model": env_model or default["model"],
            }
            set_setting("llm_providers", json.dumps(providers, ensure_ascii=False))
            logger.info(f"已用环境变量为供应商 {active} 初始化 API Key")

    Config.LLM_ACTIVE_PROVIDER = active

    # ---- 把当前供应商配置同步到 Config（各 LLM 调用点沿用） ----
    current = _current_provider_cfg(active)
    Config.DEEPSEEK_API_KEY = current["api_key"]
    Config.DEEPSEEK_BASE_URL = current["base_url"]
    Config.DEEPSEEK_MODEL = current["model"]

    webhooks = _parse_json_list(db.get("dingtalk_webhooks") or "")
    Config.DINGTALK_WEBHOOKS = webhooks

    companies = _parse_json_list(db.get("target_companies") or "")
    prompts.reload_prompts(companies)

    logger.info(
        f"运行时配置加载完成：当前供应商={active}，"
        f"API Key={'已配置' if current['api_key'] else '未配置'}，"
        f"钉钉群 {len(webhooks)} 个，监控公司 {len(companies)} 家"
    )


# ---------- 保存（供 /api/settings 接口调用） ----------

def save_llm_provider(provider: str, api_key: str = "", base_url: str = "", model: str = "") -> str:
    """
    校验并保存某个供应商的配置（局部更新：api_key 留空 = 保持不变）。

    参数：
      provider: 供应商 id（deepseek/qwen/kimi/doubao/glm，见 providers.PROVIDERS）
      api_key:  新 key。空串或空白 = 保持不变
      base_url: API 地址（留空用该供应商默认）
      model:    模型名（豆包填推理接入点 ID ep-xxx；留空用默认）

    返回：
      打码后的 key（前端展示用）

    抛错：
      ValueError：未知供应商
    """
    provider = (provider or "").strip()
    if provider not in PROVIDERS:
        raise ValueError(f"未知供应商：{provider}")

    cfg = _current_provider_cfg(provider)
    if api_key and api_key.strip():
        cfg["api_key"] = api_key.strip()
    cfg["base_url"] = (base_url or "").strip() or cfg["base_url"]
    cfg["model"] = (model or "").strip() or cfg["model"]

    providers = _parse_providers((get_settings() or {}).get("llm_providers") or "")
    providers[provider] = cfg
    set_setting("llm_providers", json.dumps(providers, ensure_ascii=False))

    # 保存的正好是当前使用的供应商 → 立即热更新 Config（不用重启）
    if provider == getattr(Config, "LLM_ACTIVE_PROVIDER", DEFAULT_PROVIDER):
        Config.DEEPSEEK_API_KEY = cfg["api_key"]
        Config.DEEPSEEK_BASE_URL = cfg["base_url"]
        Config.DEEPSEEK_MODEL = cfg["model"]

    logger.info(f"已保存供应商 {provider} 的配置")
    return mask_key(cfg["api_key"])


def set_active_provider(provider: str) -> None:
    """
    把某个供应商设为"当前使用"：写库 + 立即同步 Config（下次 LLM 调用即吃到新配置）。

    抛错：
      ValueError：未知供应商
    """
    provider = (provider or "").strip()
    if provider not in PROVIDERS:
        raise ValueError(f"未知供应商：{provider}")

    set_setting("llm_active_provider", provider)
    Config.LLM_ACTIVE_PROVIDER = provider

    cfg = _current_provider_cfg(provider)
    Config.DEEPSEEK_API_KEY = cfg["api_key"]
    Config.DEEPSEEK_BASE_URL = cfg["base_url"]
    Config.DEEPSEEK_MODEL = cfg["model"]
    logger.info(f"已切换当前供应商：{provider}（model={cfg['model'] or '待填'}）")


def get_llm_providers() -> dict:
    """
    返回所有供应商的展示信息（供设置弹窗下拉 + 编辑字段初始化）：
    {pid: {label, masked_key, base_url, model, tip}}。api_key 一律打码。
    """
    out = {}
    for pid, meta in PROVIDERS.items():
        cfg = _current_provider_cfg(pid)
        out[pid] = {
            "label": meta["label"],
            "masked_key": mask_key(cfg["api_key"]),
            "base_url": cfg["base_url"],
            "model": cfg["model"],
            "tip": meta.get("tip") or "",
        }
    return out


def save_deepseek_key(new_key: str) -> str:
    """
    （向后兼容）旧接口：保存 DeepSeek 供应商的 API Key。

    参数：
      new_key: 新 key。空串或空白 = 保持不变（返回当前打码值）

    返回：
      打码后的 key（前端展示用）

    抛错：
      ValueError：格式不合法（必须以 sk- 开头）
    """
    new_key = (new_key or "").strip()
    if new_key and not new_key.startswith("sk-"):
        raise ValueError("API Key 必须以 sk- 开头")
    return save_llm_provider("deepseek", api_key=new_key)


def save_webhooks(webhooks: list) -> list:
    """
    校验并保存钉钉群 webhook 列表（群发目标）。

    参数：
      webhooks: [{"name": "群名", "url": "https://oapi.dingtalk.com/..."}]

    返回：
      清洗后的列表（空 URL 的行被丢弃，缺名自动补"群N"）

    抛错：
      ValueError：某行 URL 不是钉钉 webhook 地址
    """
    cleaned = []
    for w in webhooks or []:
        if not isinstance(w, dict):
            continue
        name = (w.get("name") or "").strip()
        url = (w.get("url") or "").strip()
        if not url:
            continue
        if not url.startswith(_DINGTALK_URL_PREFIX):
            raise ValueError(f"webhook 必须以 {_DINGTALK_URL_PREFIX} 开头：{url[:60]}")
        cleaned.append({"name": name or f"群{len(cleaned) + 1}", "url": url})

    set_setting("dingtalk_webhooks", json.dumps(cleaned, ensure_ascii=False))
    Config.DINGTALK_WEBHOOKS = cleaned
    logger.info(f"已更新钉钉群列表：{len(cleaned)} 个群")
    return cleaned


def save_target_companies(companies: list) -> list:
    """
    校验并保存监控公司列表，并立即重建提示词常量。

    参数：
      companies: [{"name": "公司名", "region": "china|overseas",
                   "category": "ai_model|data_labeling|embodied_ai",
                   "keywords": ["别名1", "别名2"]}]

    返回：
      清洗后的列表

    抛错：
      ValueError：空公司名（整行为空则被丢弃，不会报错）
    """
    cleaned = []
    for c in companies or []:
        if not isinstance(c, dict):
            continue
        name = (c.get("name") or "").strip()
        if not name:
            continue
        region = c.get("region") if c.get("region") in _VALID_REGIONS else "china"
        category = c.get("category") if c.get("category") in _VALID_CATEGORIES else "ai_model"
        keywords = [k.strip() for k in (c.get("keywords") or []) if k and str(k).strip()]
        cleaned.append({"name": name, "region": region, "category": category, "keywords": keywords})

    set_setting("target_companies", json.dumps(cleaned, ensure_ascii=False))
    prompts.reload_prompts(cleaned)
    logger.info(f"已更新监控公司列表：{len(cleaned)} 家公司")
    return cleaned


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """直接运行 python settings.py 查看运行时配置加载结果"""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
    from database import init_database
    init_database()
    load_runtime_settings()
    print("=" * 50)
    print(f"API Key：{mask_key(Config.DEEPSEEK_API_KEY)}")
    print(f"钉钉群：{Config.DINGTALK_WEBHOOKS}")
    print(f"监控公司：{len(prompts.get_current_companies())} 家")
    print(f"预筛选关键词数：{len(prompts.COMPANY_KEYWORDS)}")
    print("=" * 50)
