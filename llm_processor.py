"""
=========================================================
LLM 处理器模块（llm_processor.py）— Stage 3 实现
=========================================================
职责：调用 DeepSeek API，对原始内容做智能筛选、分类和摘要。
=========================================================
学习注释：
  本模块是"AI 的大脑"——负责理解文章内容，判断是否相关，
  提取公司和分类，并生成中文标题和摘要。

  技术选型：使用 openai 官方 Python SDK 调用 DeepSeek API。
  因为 DeepSeek API 完全兼容 OpenAI 的接口规范，
  只需改 base_url 和 api_key 即可，无需额外适配。

  处理流程：
  1. 从数据库获取未处理的条目（is_relevant IS NULL）
  2. 逐条发送给 DeepSeek API 进行分析
  3. 解析返回的 JSON 结果
  4. 写入数据库（update_item_analysis）
  5. 记录统计：处理了多少条、多少相关/不相关
=========================================================
"""

import json
import logging
import re
import time
from typing import Optional

from openai import OpenAI

from config import Config
from database import get_unprocessed_items, update_item_analysis, delete_item
import cost_tracker
import prompts
from notifier import push_relevant_items
from dedup import cluster_near_duplicates, key_tokens

# ---------- 日志 ----------
logger = logging.getLogger("llm_processor")


# ---------- 推送前去重 ----------

def _dedup_for_push(newly_relevant: list) -> list:
    """
    推送前合并"同一事件的多源报道"（减少钉钉推送里的重复卡片）。

    原理（详见 dedup.py）：
      采集去重只在 URL 层，不同媒体报同一事件的多个链接会全部入库、
      全部被 LLM 标记相关、同批全部推送。而 LLM 生成的 ai_title 是
      统一的中文短标题——同一事件的多源报道标题高度相似（甚至跨语言），
      按 ai_title 相似度聚类即可识别。

    做法：
      1. 按 (领域, 公司) 分组，组内才聚类（不同公司的事件不混并）
      2. 组内按 ai_title 三级闸门相似度聚类
      3. 每簇保留"摘要最长"的一条做代表卡片，其余来源合并进
         "📰 同事件另有 N 个来源报道：X、Y" 备注
      4. 只影响钉钉推送——数据库 / 网页 / 日报仍保留全部报道

    返回：
      去重后的推送列表（长度 ≤ 传入长度）
    """
    if len(newly_relevant) <= 1:
        return newly_relevant

    from collections import OrderedDict
    groups = OrderedDict()
    for it in newly_relevant:
        groups.setdefault((it.get("category"), it.get("company_name")), []).append(it)

    final = []
    for _key, group in groups.items():
        clusters = cluster_near_duplicates(
            group,
            text_fn=lambda x: x.get("ai_title") or "",
            tokens_fn=lambda x: key_tokens(x.get("ai_title") or "", x.get("company_name") or ""),
            high=Config.DEDUP_SIM_HIGH,
            low_strong=Config.DEDUP_SIM_LOW_STRONG,
            low_cjk=Config.DEDUP_SIM_LOW_CJK,
        )
        for cl in clusters:
            if len(cl) == 1:
                final.append(cl[0])
                continue
            # 选摘要最长的一条做代表卡片（信息最全）；拷贝避免改动原始条目
            rep = dict(max(cl, key=lambda x: len(x.get("ai_summary") or "")))
            others = sorted(
                {c.get("source_name") for c in cl if c.get("source_name")}
                - {rep.get("source_name")}
            )
            if others:
                base = (rep.get("ai_summary") or "").strip()
                rep["ai_summary"] = f"{base}\n\n📰 同事件另有 {len(others)} 个来源报道：{'、'.join(others)}"
            final.append(rep)
    return final


# ---------- DeepSeek 客户端 ----------

def _get_client() -> Optional[OpenAI]:
    """
    创建 DeepSeek API 客户端。

    学习注释：
      openai.OpenAI 默认连接 api.openai.com，
      通过 base_url 参数指向 DeepSeek 的服务器。
      只要传对 api_key 和 base_url，SDK 就能正常工作。

      如果 API Key 未配置，返回 None 并打印提示。
    """
    if not Config.DEEPSEEK_API_KEY:
        logger.warning("DeepSeek API Key 未配置！请在 .env 中设置 DEEPSEEK_API_KEY")
        return None

    return OpenAI(
        api_key=Config.DEEPSEEK_API_KEY,
        base_url=Config.DEEPSEEK_BASE_URL,
    )


# ---------- 单条处理 ----------

def _process_single_item(client: OpenAI, item: dict, retry_count: int = 2) -> Optional[dict]:
    """
    处理单条未处理条目：发送给 DeepSeek 分析，返回解析后的结果。

    参数：
      client: DeepSeek API 客户端
      item: 数据库中的条目字典，包含 id, title, content_text, source_name
      retry_count: JSON 解析失败时的重试次数

    返回：
      解析成功的 dict，包含 is_relevant/category/company_name/ai_title/ai_summary
      失败返回 None

    学习注释：
      这个函数是 LLM 调用的核心。每条内容都要经过：
      1. 构造消息（System Prompt + User Prompt）
      2. 发送 API 请求
      3. 从回复中提取 JSON
      4. 验证 JSON 字段完整性
      5. 失败时重试

      为什么逐条处理而不是批量？
      - 每条内容长度不一，批量容易超 Token 限制
      - 如果一条出错需要重试，分批只影响一条
      - DeepSeek API 响应很快（1-3秒），逐条也来得及
    """
    title = (item.get("title") or "").strip()
    content = (item.get("content_text") or "").strip()
    source_name = (item.get("source_name") or "").strip()

    # 截断过长内容（控制 Token 消耗）
    # DeepSeek 中文约 1.5 字符 = 1 token
    # 系统提示词约 2000 tokens，留给内容约 2000 tokens ≈ 3000 汉字
    if len(content) > 3000:
        content = content[:3000]

    # 构造用户提示词
    user_prompt = prompts.USER_PROMPT_TEMPLATE.format(
        title=title,
        source_name=source_name,
        content=content or "(无内容)",
    )

    logger.info(f"正在分析：{title[:50]}...")

    for attempt in range(retry_count + 1):
        try:
            # ---- 调用 DeepSeek API ----
            response = client.chat.completions.create(
                model=Config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": prompts.SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,  # 低温度让输出更稳定、格式更可靠
                max_tokens=500,   # JSON 输出不需要太多 token
            )
            # 记账：每次成功响应按 token 用量累计当日成本（超限后入口门禁拦截）
            cost_tracker.record_usage(response.usage)

            # ---- 提取回复文本 ----
            reply = response.choices[0].message.content
            if not reply:
                logger.warning(f"  → API 返回空内容")
                if attempt < retry_count:
                    time.sleep(1)
                    continue
                return None

            # ---- 解析 JSON ----
            result = _parse_json_response(reply)
            if result is not None:
                return result

            # JSON 解析失败，重试
            if attempt < retry_count:
                logger.warning(f"  → JSON 解析失败，重试第 {attempt + 1} 次...")
                time.sleep(1)
            else:
                logger.error(f"  → JSON 解析失败，已达最大重试次数。原始回复: {reply[:200]}")
                return None

        except Exception as e:
            error_msg = str(e)
            if "rate_limit" in error_msg.lower() or "429" in error_msg:
                # 速率限制：等久一点再重试
                wait = (attempt + 1) * 5
                logger.warning(f"  → API 速率限制，等待 {wait} 秒...")
                time.sleep(wait)
            elif "timeout" in error_msg.lower():
                logger.warning(f"  → API 超时，重试...")
                time.sleep(2)
            elif "connection" in error_msg.lower():
                logger.warning(f"  → 网络连接错误：{error_msg[:80]}")
                time.sleep(2)
            else:
                logger.error(f"  → API 调用失败：{error_msg[:120]}")
                if attempt < retry_count:
                    time.sleep(1)
                else:
                    return None

    return None


def _normalize_company_name(raw: str) -> str:
    """
    清洗公司名：去掉括号注释，统一用简称。

    例如：
      "DeepSeek（深度求索）、xAI (Elon Musk)" → "DeepSeek、xAI"
      "阿里（通义千问）" → "阿里"

    支持中文全角括号（）和英文半角括号 ()。
    """
    if not raw:
        return raw

    import re
    parts = raw.split("、")
    cleaned = []
    for part in parts:
        # 去掉中文括号及内容：（...）
        part = re.sub(r"（[^）]*）", "", part)
        # 去掉英文括号及内容： (...)
        part = re.sub(r"\([^)]*\)", "", part)
        part = part.strip()
        if part:
            cleaned.append(part)
    return "、".join(cleaned)


def _is_company_whitelisted(company_name: str) -> Optional[str]:
    """
    白名单校验（模型无关，2026-08-24 新增）：
    判断 company_name 是否在监控公司名单内（命中规范名或其关键词别名），
    命中返回规范名（别名回写为规范名，防同一公司分组分裂），否则返回 None。

    设计说明：
      - 名单从 prompts.get_current_companies() 动态读取，随设置弹窗增删即时生效
        （与 SYSTEM_PROMPT 同一数据源，不会出现"提示词里有、校验名单里没有"）。
      - 每次调用动态构建查找表：40 家规模微秒级，远小于一次 API 调用，故意
        不加缓存，避免引入模块级状态，保持零架构影响。
      - 英文名称/别名一律小写匹配，大小写不敏感。
    """
    if not company_name:
        return None
    lower = company_name.strip().lower()
    companies = prompts.get_current_companies()
    canonical = {}
    alias = {}
    for c in companies:
        name = (c.get("name") or "").strip()
        if not name:
            continue
        canonical[name.lower()] = name
        for kw in c.get("keywords") or []:
            kw = str(kw).strip()
            if kw:
                alias[kw.lower()] = name
    if lower in canonical:
        return canonical[lower]
    if lower in alias:
        return alias[lower]
    return None


def _company_region_category(name: str) -> tuple:
    """按规范名查白名单名单，返回该公司的 (region, category)；不在名单返回 (None, None)。"""
    name = (name or "").strip()
    for c in prompts.get_current_companies():
        if (c.get("name") or "").strip() == name:
            return c.get("region"), c.get("category")
    return None, None


def _ground_companies_in_text(text: str) -> list:
    """正文证据校验（模型无关，2026-08-25 新增）：
    返回在 text 中明确出现（命中规范名或关键词别名）的白名单公司规范名，按名单顺序。

    用于剥离 LLM 凭背景知识/联想输出的"正文里根本没有"的公司标签。
    注意：与 quick_filter 一致用子串匹配；英文短名（如 Meta 命中 metadata）会产生
    少量"该判没判"的漏报，方向安全（不误删、不误改），不做词边界处理以免引入复杂度。
    """
    if not text:
        return []
    t = text.lower()
    grounded = []
    for c in prompts.get_current_companies():
        name = (c.get("name") or "").strip()
        if not name:
            continue
        keys = [name] + [str(k) for k in (c.get("keywords") or []) if str(k).strip()]
        if any(k.lower() in t for k in keys):
            grounded.append(name)
    return grounded


def _extract_json(reply: str) -> Optional[str]:
    """
    从 LLM 回复中提取 JSON 文本，返回可直接 json.loads 的字符串。

    学习注释：
      LLM 输出的 JSON 可能被包在 ```json ... ``` 代码块中，
      也可能带前缀/后缀文字。这里先清理代码块标记，
      再尝试完整解析；失败时按最外层数组 [ ] 或对象 { } 找边界。

      单篇模式返回对象，批量模式返回数组，所以两个方向都支持。
    """
    # ---- 清理 Markdown 代码块标记 ----
    reply = reply.strip()

    if "```json" in reply:
        # 找到 ```json 和对应的 ``` 之间内容
        start = reply.find("```json") + 7
        end = reply.find("```", start)
        if end > start:
            reply = reply[start:end].strip()
    elif reply.startswith("```"):
        # 找到 ``` 和对应的 ``` 之间内容
        start = reply.find("```") + 3
        end = reply.find("```", start)
        if end > start:
            reply = reply[start:end].strip()

    # ---- 尝试直接完整解析 ----
    try:
        json.loads(reply)
        return reply
    except json.JSONDecodeError:
        pass

    # ---- 尝试找最外层数组（批量模式） ----
    start = reply.find("[")
    end = reply.rfind("]")
    if start >= 0 and end > start:
        candidate = reply[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    # ---- 尝试找最外层对象（单篇模式） ----
    start = reply.find("{")
    end = reply.rfind("}")
    if start >= 0 and end > start:
        candidate = reply[start:end + 1]
        try:
            json.loads(candidate)
            return candidate
        except json.JSONDecodeError:
            pass

    return None


# 相关性交叉校验（修复 2026-08-17 相关性 bug）：
# 曾出现 LLM 返回 is_relevant=true、摘要却写"未提及任何目标公司，故不判定为相关"
# 的自相矛盾结果，代码只信布尔值导致这类条目入库并推送。
# 这里用保守正则匹配"摘要自带不相关评语"的典型表述（宁可漏匹配也不误杀——
# 避免"无关"这种可能出现在正常句子里的词造成误删）。
_NOT_RELEVANT_SUMMARY_RE = re.compile(
    r"未提(及|到).{0,6}目标公司"
    r"|未涉及.{0,6}目标公司"
    r"|不判定为相关"
    r"|故不相关"
    r"|判断为不相关"
    r"|判定为不相关"
)


def _validate_result_dict(data) -> Optional[dict]:
    """
    校验并规范化单个 LLM 分析结果字典。

    学习注释：
      单篇和批量数组里的每个元素都走这个函数。
      非 dict 直接返回 None，字段缺失/非法时用安全默认值兜底，
      不能让 LLM 漏一个字段就导致数据库写入失败。
    """
    if not isinstance(data, dict):
        return None

    is_relevant = data.get("is_relevant", False)
    # 兼容各种 true/false 表示
    if isinstance(is_relevant, str):
        is_relevant = is_relevant.lower() in ("true", "yes", "是", "1")

    category = data.get("category", "")
    # 验证 category 是否合法
    valid_categories = ("ai_model", "data_labeling", "embodied_ai")
    if category not in valid_categories:
        category = "" if not is_relevant else "ai_model"  # 默认归入 AI 大模型

    company_name = data.get("company_name", "")
    ai_title = data.get("ai_title", "")
    ai_summary = data.get("ai_summary", "")

    # 公司地区：china（国内公司）| overseas（海外公司），按公司归属判定
    region = data.get("region", "")
    valid_regions = ("china", "overseas")
    if region not in valid_regions:
        region = ""  # 非法/缺失 → 置空（不出现在地区筛选里，但仍显示在"全部"）

    # 确保都是字符串
    company_name = str(company_name) if company_name else ""
    ai_title = str(ai_title) if ai_title else ""
    ai_summary = str(ai_summary) if ai_summary else ""

    # 清洗公司名：去掉括号注释，统一用简称
    company_name = _normalize_company_name(company_name)

    # 防御性交叉校验（2026-08-17 修复相关性 bug）：
    # LLM 判定相关但摘要自带"未提及目标公司/不相关"评语 = 自相矛盾，
    # 强制改判不相关（下游走删除路径，不再产生"自相矛盾入库 + 推送"）。
    if is_relevant and ai_summary and _NOT_RELEVANT_SUMMARY_RE.search(ai_summary):
        logger.warning(
            f"交叉校验：判定相关但摘要含不相关评语，强制改判不相关"
            f"（摘要：{ai_summary[:50]}…）"
        )
        is_relevant = False

    # 硬规则一致性校验（模型无关，2026-08-18 新增）：
    # 判定「相关」的唯一标准是涉及至少一家目标公司；is_relevant=true 但
    # company_name 为空 = 违反硬规则（换模型后千问等可能把"国产算力/基础设施"
    # 类文章判相关却无目标公司，还常伴空标题），一律强制改判不相关，不产生
    # "无公司却入库 + 推送"的矛盾记录。
    if is_relevant and not company_name:
        logger.warning(
            f"硬规则校验：判定相关但无目标公司，强制改判不相关"
            f"（ai_title：{ai_title[:40]}…）"
        )
        is_relevant = False
        category = ""
        region = ""

    # 硬规则一致性校验（模型无关，2026-08-24 新增）：
    # company_name 必须在监控名单内（命中规范名或关键词别名）。弱模型可能输出
    # 名单外的编造公司（批内串扰 / 联想编造，如"加速进化"），一律强制改判
    # 不相关，不产生"名单外公司标签 + 入库推送"的矛盾记录。
    if is_relevant:
        canonical = _is_company_whitelisted(company_name)
        if canonical is None:
            logger.warning(
                f"硬规则校验：company_name 不在监控名单内，强制改判不相关"
                f"（company_name：{company_name[:30]}…）"
            )
            is_relevant = False
            category = ""
            region = ""
            company_name = ""
        else:
            company_name = canonical

    return {
        "is_relevant": bool(is_relevant),
        "category": category,
        "company_name": company_name,
        "region": region,
        "ai_title": ai_title,
        "ai_summary": ai_summary,
    }


def _extract_json_data(reply: str):
    """
    从 LLM 回复中提取 JSON 并解析成 Python 对象（dict 或 list）。

    单篇路径要对象、批量路径要数组，两者"提取文本 → 解析"前半段完全一样，
    抽出来共用；提取失败或解析失败统一返回 None。
    """
    json_text = _extract_json(reply)
    if json_text is None:
        return None
    try:
        return json.loads(json_text)
    except json.JSONDecodeError:
        return None


def _parse_json_response(reply: str) -> Optional[dict]:
    """
    从 LLM 回复中提取并验证单篇 JSON 分析结果。

    学习注释：
      单篇路径的函数名保持不变，内部复用 _extract_json_data + _validate_result_dict，
      保证改造前后行为完全一致（零回归）。
    """
    data = _extract_json_data(reply)
    return _validate_result_dict(data) if isinstance(data, dict) else None


def _parse_batch_response(reply: str, expected_count: int) -> Optional[list]:
    """
    从 LLM 回复中提取并验证批量 JSON 数组结果。

    参数：
      reply: LLM 的原始回复文本
      expected_count: 本次批量包含的文章数

    返回：
      - 解析失败（非数组 / 提取不到）→ None，调用方走整批降级
      - 成功 → 长度等于 expected_count 的列表，逐个元素是结果 dict 或 None
        （单个元素非法时置 None，调用方对该元素走单条降级）
    """
    data = _extract_json_data(reply)
    if not isinstance(data, list):
        return None

    results = []
    for elem in data[:expected_count]:
        # 单个元素无效 → None（交给调用方做元素级降级，保留重试机会）
        results.append(_validate_result_dict(elem) if isinstance(elem, dict) else None)

    # 数组不足 expected_count 时补齐 None（防止 zip 静默少处理）
    while len(results) < expected_count:
        results.append(None)

    return results


def _build_batch_user_prompt(items) -> str:
    """
    构建批量分析的用户提示词：多篇文章逐篇编号。

    正文截到 2000 字符（与 collector 入库截断一致，无信息损失），
    空正文用"(无内容)"占位（与单条路径一致）。
    """
    blocks = []
    for i, item in enumerate(items, 1):
        title = (item.get("title") or "").strip()
        source = (item.get("source_name") or "").strip()
        content = (item.get("content_text") or "").strip()
        if len(content) > 2000:
            content = content[:2000]
        blocks.append(
            prompts.BATCH_USER_PROMPT_ARTICLE.format(
                index=i,
                title=title,
                source_name=source,
                content=content or "(无内容)",
            )
        )

    header = prompts.BATCH_USER_PROMPT_HEADER.format(count=len(items))
    footer = prompts.BATCH_USER_PROMPT_FOOTER.format(count=len(items))
    return header + "\n".join(blocks) + footer


def _process_batch(client: OpenAI, items: list, retry_count: int = 2) -> Optional[list]:
    """
    批量分析多条文章：一次 API 调用，返回与输入顺序对应的结果列表。

    参数：
      client: DeepSeek API 客户端
      items: 数据库条目字典列表（这一批要分析的文章）
      retry_count: 失败时的重试次数

    返回：
      成功 → 长度等于 len(items) 的列表（元素是结果 dict 或 None）
      整批失败（截断 / 解析失败 / 网络错误重试尽）→ None，调用方走逐条降级

    学习注释：
      max_tokens=8192 是 DeepSeek 的硬上限，批量输出需要更大预算。
      finish_reason == "length" 表示输出被截断——此时整段 JSON 数组已经损坏，
      必须整批降级，而不是硬解析截断的半截数组。
    """
    batch_size = len(items)
    user_prompt = _build_batch_user_prompt(items)

    logger.info(f"批量分析 {batch_size} 篇...")

    for attempt in range(retry_count + 1):
        try:
            # ---- 调用 DeepSeek API ----
            response = client.chat.completions.create(
                model=Config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": prompts.BATCH_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=8192,  # DeepSeek 硬上限，批量输出预留
            )
            # 记账：批量请求 token 更大，按实际用量累计当日成本
            cost_tracker.record_usage(response.usage)

            # ---- 截断检测：输出被截断 = 数组已损坏 = 整批失败 ----
            finish_reason = response.choices[0].finish_reason
            if finish_reason == "length":
                logger.warning(f"  → 批量输出被截断（max_tokens 不够），整批失败")
                if attempt < retry_count:
                    time.sleep(1)
                    continue
                return None

            # ---- 提取回复文本 ----
            reply = response.choices[0].message.content
            if not reply:
                logger.warning("  → API 返回空内容")
                if attempt < retry_count:
                    time.sleep(1)
                    continue
                return None

            # ---- 解析批量 JSON 数组 ----
            results = _parse_batch_response(reply, expected_count=batch_size)
            if results is not None:
                return results

            # JSON 解析失败，重试
            if attempt < retry_count:
                logger.warning(f"  → 批量 JSON 解析失败，重试第 {attempt + 1} 次...")
                time.sleep(1)
            else:
                logger.error(f"  → 批量 JSON 解析失败，已达最大重试次数。原始回复: {reply[:200]}")
                return None

        except Exception as e:
            error_msg = str(e)
            if "rate_limit" in error_msg.lower() or "429" in error_msg:
                # 速率限制：批量请求更重，等待时间按批放大
                wait = (attempt + 1) * 10
                logger.warning(f"  → API 速率限制，等待 {wait} 秒...")
                time.sleep(wait)
            elif "timeout" in error_msg.lower():
                logger.warning("  → API 超时，重试...")
                time.sleep(2)
            elif "connection" in error_msg.lower():
                logger.warning(f"  → 网络连接错误：{error_msg[:80]}")
                time.sleep(2)
            else:
                logger.error(f"  → API 调用失败：{error_msg[:120]}")
                if attempt < retry_count:
                    time.sleep(1)
                else:
                    return None

    return None


# ---------- 批量处理入口 ----------

def process_unprocessed_items(max_items: int = 20) -> int:
    """
    批量处理数据库中未处理的条目。

    参数：
      max_items: 一次最多处理多少条（会向上取整到整批，避免不满批的尾巴）

    返回：
      成功处理的条目数

    学习注释：
      这是 LLM 处理器的"总入口"。每次被调用时：
      1. 从数据库拿一批未处理的条目
      2. 关键词预筛选，丢掉确定无关的（省 LLM 费用）
      3. 剩下的按 Config.LLM_BATCH_SIZE 切成若干批，每批一次 API 调用
      4. 将分析结果写回数据库，相关条目收集起来供钉钉推送
      5. 返回成功处理的数量

      max_items 向上取整到整批：max_items=20、batch=8 时会取 24 条 → 8/8/8，
      避免最后一批发不满而重复付一份系统提示词的输入费。
    """
    # ---- 检查 API Key ----
    client = _get_client()
    if client is None:
        logger.error("无法初始化 DeepSeek 客户端，请检查 DEEPSEEK_API_KEY 配置")
        return 0

    # ---- 每日成本上限门禁（超限 → 今日自动暂停，次日恢复） ----
    if cost_tracker.is_blocked():
        logger.warning("今日 LLM 成本已达上限，跳过本轮处理（次日自动恢复）")
        return 0

    # ---- 本轮上限向上取整到整批 ----
    batch_size = Config.LLM_BATCH_SIZE
    cap = ((max_items + batch_size - 1) // batch_size) * batch_size

    # ---- 获取未处理条目 ----
    items = get_unprocessed_items(limit=cap)
    if not items:
        logger.info("没有待处理的条目")
        return 0

    logger.info(f"开始 LLM 处理：本次 {len(items)} 条待处理")

    processed_count = 0
    relevant_count = 0
    irrelevant_count = 0
    prefiltered_count = 0  # 关键词预筛选直接丢弃的
    newly_relevant = []  # 本轮新标记为相关的条目（供钉钉推送用）

    # ---- ① 关键词预筛选前置（第一道防线，不用 LLM）----
    # 先砍掉确定无关的，只让候选进入批量，避免垃圾文章白占批量输入 token
    candidates = []
    for item in items:
        item_id = item["id"]
        title = (item.get("title") or "").strip()
        if not prompts.quick_filter(title, item.get("content_text") or ""):
            try:
                delete_item(item_id)
            except Exception:
                pass
            prefiltered_count += 1
            logger.info(f"  → 🗑️ 关键词预筛选不通过，直接丢弃")
            continue
        candidates.append(item)

    if not candidates:
        logger.info(f"LLM 处理完成：{len(items)} 条全部被关键词预筛选丢弃，无候选")
        return prefiltered_count

    # ---- ② 按批批量分析 ----
    for start in range(0, len(candidates), batch_size):
        chunk = candidates[start:start + batch_size]
        logger.info(f"处理批次 {start // batch_size + 1}：{len(chunk)} 篇")

        results = _process_batch(client, chunk)
        if results is None:
            # 整批失败 → 逐条降级（保留单条路径自己的重试）
            logger.warning("  → 整批失败，降级为逐条分析")
            results = [_process_single_item(client, item) for item in chunk]

        for item, result in zip(chunk, results):
            item_id = item["id"]
            title_preview = (item.get("title") or "").strip()[:40]

            # 单个元素无效 → 元素级降级（与单条"重试 2 次才删"语义一致）
            if result is None:
                logger.warning(f"  → 批量中 #{item_id} 结果无效，逐条降级重试")
                result = _process_single_item(client, item)

            if result is None:
                # 处理失败，直接删除（无法判断是否相关，留着也没用）
                try:
                    delete_item(item_id)
                except Exception:
                    pass
                logger.warning(f"  → 处理失败，已删除 #{item_id} {title_preview}")
                continue

            # ---- 正文证据校验（模型无关，2026-08-25 新增）----
            # 剥离 LLM 凭背景知识/联想输出的"正文里根本没出现"的公司标签。
            # claimed = 模型输出的公司；grounded = 正文里真正出现（规范名或别名）的白名单公司。
            #   ① kept = claimed ∩ grounded 非空 → 只保留 kept（剔除无证据的 claimed）
            #   ② kept 空但 grounded 恰好一家 → 定向回写（case 9：正文只出现梁文锋，
            #      智谱AI 回写为 DeepSeek，需先在设置里把"梁文锋"挂到 DeepSeek 别名下）
            #   ③ kept/grounded 均空，或 grounded 多家无法定向 → 无证据，强制改判不相关
            #      （case 10；宁可漏判不可误判）
            if result["is_relevant"] and result.get("company_name"):
                claimed = [c.strip() for c in result["company_name"].split("、") if c.strip()]
                if claimed:
                    grounded = _ground_companies_in_text(
                        f"{item.get('title') or ''} {item.get('content_text') or ''}"
                    )
                    kept = [c for c in claimed if c in grounded]
                    if kept:
                        if len(kept) == 1:
                            region, category = _company_region_category(kept[0])
                            if region:
                                result["region"] = region
                            if category:
                                result["category"] = category
                        result["company_name"] = "、".join(kept)
                    elif len(grounded) == 1:
                        new = grounded[0]
                        region, category = _company_region_category(new)
                        logger.warning(
                            f"正文证据校验：{result['company_name'][:30]}… 在正文无证据，"
                            f"按正文唯一证据回写为 {new}"
                        )
                        result["company_name"] = new
                        if region:
                            result["region"] = region
                        if category:
                            result["category"] = category
                    else:
                        logger.warning(
                            f"正文证据校验：company_name 在正文中无证据，强制改判不相关"
                            f"（company_name：{result['company_name'][:30]}…）"
                        )
                        result["is_relevant"] = False
                        result["category"] = ""
                        result["region"] = ""

            # ---- 写入数据库（相关）或删除（不相关）----
            try:
                if result["is_relevant"]:
                    update_item_analysis(
                        item_id=item_id,
                        is_relevant=True,
                        ai_title=result["ai_title"],
                        ai_summary=result["ai_summary"],
                        category=result["category"],
                        company_name=result["company_name"],
                        region=result["region"],
                    )
                    processed_count += 1
                    relevant_count += 1
                    # 收集相关条目数据，供钉钉推送（带发布时间，卡片会显示分钟级时间）
                    newly_relevant.append({
                        "ai_title": result["ai_title"],
                        "ai_summary": result["ai_summary"],
                        "title": item.get("title", ""),  # 原文标题，推送侧 ai_title 为空时回退用
                        "category": result["category"],
                        "company_name": result["company_name"],
                        "region": result["region"],
                        "source_name": item.get("source_name", ""),
                        "source_url": item.get("source_url", ""),
                        "published_at": item.get("published_at", ""),
                    })
                    logger.info(f"  → ✅ 相关 [{result['category']}] {result['ai_title'][:30]}")
                else:
                    # 不相关直接删除，不保留
                    delete_item(item_id)
                    irrelevant_count += 1
                    logger.info(f"  → ❌ 不相关，已删除")
            except Exception as e:
                logger.error(f"  → 数据库写入失败：{e}")

        # 批间成本检查：本轮进行中成本已超上限 → 停止后续批次（避免跑过头太多）
        if cost_tracker.is_blocked():
            logger.warning("本轮处理中 LLM 成本已达上限，停止后续批次（次日自动恢复）")
            break

        # 批间请求间隔（避免触发速率限制）
        # DeepSeek 免费/低价 API 可能有限流，批间 0.5 秒比较安全
        time.sleep(0.5)

    # ---- ③ 钉钉推送（独立于主流程，失败不影响） ----
    if newly_relevant:
        try:
            push_items = newly_relevant
            if Config.PUSH_DEDUP_ENABLED:
                push_items = _dedup_for_push(newly_relevant)
                if len(push_items) != len(newly_relevant):
                    logger.info(
                        f"推送去重：{len(newly_relevant)} 条 → {len(push_items)} 张卡片"
                        f"（同事件多源报道已合并）"
                    )
            push_relevant_items(push_items)
        except Exception:
            logger.warning("钉钉推送异常（不影响 LLM 处理结果）", exc_info=True)

    logger.info(
        f"LLM 处理完成：总计 {len(items)} 条，"
        f"预筛选丢弃 {prefiltered_count} 条，"
        f"相关收录 {relevant_count} 条，"
        f"LLM判定不相关删除 {irrelevant_count} 条，"
        f"钉钉推送 {len(newly_relevant)} 条"
    )
    return processed_count + irrelevant_count + prefiltered_count


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python llm_processor.py 可以单独测试 LLM 处理功能。

    学习注释：
      独立运行 LLM 处理器，方便调试 prompt 效果。
      可以反复运行，处理完的条目不会再被处理（is_relevant 不再是 NULL）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    from database import init_database
    init_database()

    print("=" * 50)
    print("开始测试 LLM 处理功能...")
    print("=" * 50)
    count = process_unprocessed_items(max_items=5)
    print("=" * 50)
    print(f"测试完成！处理了 {count} 条。")
    print("=" * 50)
