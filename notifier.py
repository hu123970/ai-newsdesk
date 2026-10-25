"""
=========================================================
钉钉推送模块（notifier.py）
=========================================================
职责：将 LLM 处理后的相关新闻推送到钉钉群聊机器人。
=========================================================
学习注释：
  本模块是"消息触达层"——LLM 处理完新闻后，
  除了写数据库供前端展示，还通过钉钉 Webhook 推送到群聊。

  设计原则：
  1. 独立模块：不依赖其他业务模块（llm_processor/database/main）
  2. 静默降级：webhook 未配置或推送失败 → 不阻断主流程
  3. 批量合并：一个批次发一条合并消息，避免消息轰炸

  钉钉机器人消息类型选择 Markdown：
  - 支持标题、加粗、链接、引用等富文本
  - 相比纯文本更易读，相比 FeedCard 不需要图片 URL
  - 单条消息最长 4096 字符，足够一个批次的内容
=========================================================
"""

import json
import logging
from typing import List, Optional

import httpx

from config import Config

# ---------- 日志 ----------
logger = logging.getLogger("notifier")

# ---------- 钉钉消息长度上限 ----------
# 钉钉 Markdown 消息 title 最长 256，text 最长 4096
MAX_TITLE_LEN = 256
MAX_TEXT_LEN = 4096

# 钉钉群机器人在安全设置里配了必含关键词「AI 行业动态」，消息不含它会被
# 拒收（errcode=310000）。所有新推送（每日新闻/日报/微博cookie提醒/密码
# 重置码）标题和正文都必须带上，故统一收敛到这一个常量。
DINGTALK_KEYWORD = "AI 行业动态"

# ---------- 分类中文标签 ----------
CATEGORY_LABELS = {
    "ai_model": "AI大模型",
    "data_labeling": "数据标注",
    "embodied_ai": "具身智能",
}


def _format_single_card(item: dict, index: int) -> str:
    """
    将单条新闻格式化为钉钉 Markdown 卡片片段。

    参数：
      item: 包含 ai_title, ai_summary, category, company_name, source_name, source_url 的字典
            （ai_title 为空时回退 title 原文标题，与前端 ai_title or title 的兜底一致）
      index: 序号

    返回：
      格式化的 Markdown 文本片段
    """
    # 标题优先用 ai_title（LLM 生成的中文短标题）；
    # 为空时回退原文标题（item["title"]，与前端 ai_title or title 的兜底一致），
    # 避免模型漏给标题时钉钉卡片出现 "**N. **" 空标题。
    title = (item.get("ai_title") or item.get("title") or "").strip()
    summary = (item.get("ai_summary") or "").strip()
    category = (item.get("category") or "").strip()
    company = (item.get("company_name") or "").strip()
    source = (item.get("source_name") or "").strip()
    url = (item.get("source_url") or "").strip()

    category_label = CATEGORY_LABELS.get(category, category)

    # 发布时间：分钟级、统一 +08:00（naive 按 UTC 处理，混存偏移都能归一）
    from timeutil import format_minute_shanghai
    pub_time = format_minute_shanghai(item.get("published_at"))

    # 组装标签行（发布时间放最前，一眼看到这条是什么时候的）
    tags = []
    if pub_time:
        tags.append(f"🕐 {pub_time}")
    if category_label:
        tags.append(f"📂 {category_label}")
    if company:
        tags.append(f"🏢 {company}")
    if source:
        tags.append(f"📰 {source}")
    tag_line = " | ".join(tags) if tags else ""

    blocks = [f"**{index}. {title}**"]
    if tag_line:
        blocks.append(tag_line)
    if summary:
        blocks.append(summary)
    if url:
        blocks.append(f"[阅读原文]({url})")

    # 钉钉 Markdown 换行的坑（实测，2026-08-05）：
    #   1. 单个 \n 会被折叠成空格 → 整张卡片"标题 标签 正文 链接"糊成一行
    #   2. 行尾双空格硬换行只在部分钉钉客户端生效，不稳定
    #   3. <br/> 在安卓/鸿蒙端不换行
    # 唯一确定有效的换行是"空行分段"：每个元素之间用 \n\n 隔开，
    # 钉钉一定会渲染成独立段落，绝不折叠。代价是段间距略大，
    # 但"分行明确"是硬需求，牺牲一点紧凑度换确定的可读性。
    return "\n\n".join(blocks)


def _build_markdown_message(items: List[dict]) -> dict:
    """
    将一批新闻构建为钉钉 Markdown 消息体。

    参数：
      items: 已处理的新闻条目列表（每个都是 update_item_analysis 用的数据）

    返回：
      钉钉 webhook 请求的 JSON body
    """
    count = len(items)

    # 标题
    title = f"🤖 AI 行业动态 · 新入库 {count} 条"

    # 正文
    body_lines = [f"## {title}", ""]
    for i, item in enumerate(items, 1):
        body_lines.append(_format_single_card(item, i))
        body_lines.append("")  # 卡片之间也空一行，避免相邻卡片折叠

    text = "\n".join(body_lines)

    # 超长截断保护
    if len(text) > MAX_TEXT_LEN:
        text = text[:MAX_TEXT_LEN - 50] + "\n\n> ⚠️ 内容过长已截断，请访问网页查看完整列表"

    return {
        "msgtype": "markdown",
        "markdown": {
            "title": title[:MAX_TITLE_LEN],
            "text": text,
        },
    }


def _push_one(url: str, body: dict) -> bool:
    """
    推送一条消息到单个钉钉群 webhook。

    返回：
      True 推送成功，False 推送失败（含超时/异常/钉钉返回错误）

    学习注释：
      单个群独立 try/except —— 群发时一个群失效不影响其他群。
    """
    try:
        with httpx.Client(timeout=Config.NOTIFIER_TIMEOUT) as client:
            response = client.post(
                url,
                json=body,
                headers={"Content-Type": "application/json"},
            )

        if response.status_code == 200:
            resp_data = response.json()
            errcode = resp_data.get("errcode", -1)
            if errcode == 0:
                return True
            else:
                errmsg = resp_data.get("errmsg", "未知错误")
                logger.error(f"钉钉推送失败：errcode={errcode}, errmsg={errmsg}")
                return False
        else:
            logger.error(f"钉钉推送 HTTP 错误：status={response.status_code}, body={response.text[:200]}")
            return False

    except httpx.TimeoutException:
        logger.error(f"钉钉推送超时（{Config.NOTIFIER_TIMEOUT}秒）")
        return False
    except Exception:
        logger.error("钉钉推送异常", exc_info=True)
        return False


def _broadcast(body: dict, success_label: str = "✅ 钉钉推送成功",
               fail_label: str = "❌ 钉钉推送失败", count_note: str = "") -> bool:
    """
    把同一消息体逐群推送到所有已配置 webhook（群发）。

    参数：
      body:        钉钉 webhook 请求体
      success_label / fail_label: 成功/失败日志前缀（调用方按用途区分）
      count_note:  成功日志的尾部备注（如 "：N 条"）

    返回：
      True 至少一个群成功，False 全部失败；未配置 webhook 时跳过并返回 True。

    学习注释：
      空 webhook 检查 + 逐群发送循环在新闻推送和日报推送里完全一样，
      抽出来共用，保证两类推送的行为一致（群发、单群失败不拖垮其他群）。
    """
    webhooks = getattr(Config, "DINGTALK_WEBHOOKS", None) or []
    if not webhooks:
        logger.debug("未配置钉钉 webhook，跳过推送")
        return True
    any_success = False
    for w in webhooks:
        name = (w.get("name") or "").strip() or "未命名群"
        url = (w.get("url") or "").strip()
        if not url:
            logger.warning(f"跳过空 webhook：{name}")
            continue
        try:
            if _push_one(url, body):
                any_success = True
                logger.info(f"{success_label}（{name}）{count_note}")
            else:
                logger.error(f"{fail_label}（{name}）")
        except Exception:
            logger.error(f"{fail_label}异常（{name}）", exc_info=True)
    return any_success


def push_relevant_items(items: List[dict]) -> bool:
    """
    推送一批相关新闻到所有已配置的钉钉群（群发）。

    参数：
      items: 本批次新标记为相关的新闻条目列表。
             每条需包含：ai_title, ai_summary, category, company_name,
                        source_name, source_url
             如果为空列表，直接返回（不发消息）。

    返回：
      True 至少一个群推送成功或跳过，False 全部群都失败

    设计决策：
      - 空列表不推送：LLM 处理完发现全是不相关时，不发空消息
      - 未配置 webhook 不推送：向后兼容，不给现有用户添麻烦
      - 逐群隔离：一个群失败只记日志，不影响其他群和 LLM 主流程
      - 群发目标列表 Config.DINGTALK_WEBHOOKS 由 settings.py 维护
        （前端设置弹窗可增删，首次启动从 .env 种子化）
    """
    # ---- 空列表检查 ----
    if not items:
        logger.debug("push_relevant_items: 空列表，跳过推送")
        return True

    # ---- 构建消息（所有群共用同一份），逐群发送 ----
    body = _build_markdown_message(items)
    logger.info(f"准备推送 {len(items)} 条新闻到 {len(getattr(Config, 'DINGTALK_WEBHOOKS', None) or [])} 个钉钉群...")
    return _broadcast(body, count_note=f"：{len(items)} 条")


def push_report_markdown(markdown_text: str, title: str = "", button: Optional[dict] = None) -> bool:
    """
    推送一条 markdown 消息到所有已配置的钉钉群（群发）。

    用途：每日日报、密码重置码等"定时/被动触达"消息，规则与
    push_relevant_items 一致——所有群都发、单群失败不拖垮其他群。

    参数：
      markdown_text: 消息正文（markdown），须已含关键词 DINGTALK_KEYWORD
      title:        消息标题（默认截取正文第一行）
      button:       可选，{"title": "...", "url": "https://..."}。提供时改用
                    actionCard 消息类型，正文尾部挂一个跳转按钮（如「📖 查看完整日报」）
                    钉钉不支持附件发文件，长内容一律"摘要 + 链接"落地。

    返回：
      True 至少一个群推送成功，False 全部失败或未配置 webhook。
    """
    if not markdown_text:
        return True

    if not title:
        first_line = markdown_text.strip().split("\n", 1)[0]
        title = first_line.replace("#", "").strip()[:MAX_TITLE_LEN] or DINGTALK_KEYWORD

    if button and button.get("title") and button.get("url"):
        body = {
            "msgtype": "actionCard",
            "actionCard": {
                "title": title[:MAX_TITLE_LEN],
                "text": markdown_text[:MAX_TEXT_LEN],
                "btnOrientation": "0",
                "btns": [{"title": button["title"], "actionURL": button["url"]}],
            },
        }
    else:
        body = {
            "msgtype": "markdown",
            "markdown": {"title": title[:MAX_TITLE_LEN], "text": markdown_text[:MAX_TEXT_LEN]},
        }

    return _broadcast(body, success_label="✅ 日报/消息推送成功", fail_label="❌ 日报/消息推送失败")


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python notifier.py 测试钉钉推送功能。

    需要有有效的 DINGTALK_WEBHOOK_URL 配置。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 加载运行时配置（webhook 群列表来自数据库 settings 表）
    from settings import load_runtime_settings
    load_runtime_settings()

    if not Config.DINGTALK_WEBHOOKS:
        print("=" * 50)
        print("⚠️  未配置钉钉群 webhook")
        print("   请在设置弹窗或 .env 中配置后重试")
        print("=" * 50)
    else:
        # 用假数据测试格式
        test_items = [
            {
                "ai_title": "OpenAI 发布 GPT-5 预览版",
                "ai_summary": "OpenAI 今日发布了 GPT-5 预览版，在推理能力和多模态理解方面实现重大突破。新模型在 MMLU 基准测试中达到 92.5 分。",
                "category": "ai_model",
                "company_name": "OpenAI、微软",
                "source_name": "TechCrunch",
                "source_url": "https://example.com/1",
            },
            {
                "ai_title": "宇树科技发布新一代人形机器人 H1 Pro",
                "ai_summary": "宇树科技发布 H1 Pro 人形机器人，搭载自研关节电机，运动控制精度提升 40%。已在工厂物流场景落地测试。",
                "category": "embodied_ai",
                "company_name": "宇树科技",
                "source_name": "36氪",
                "source_url": "https://example.com/2",
            },
            {
                "ai_title": "Scale AI 获 10 亿美元融资",
                "ai_summary": "Scale AI 宣布完成新一轮 10 亿美元融资，估值达 140 亿美元。公司将加大合成数据生成方向的投入。",
                "category": "data_labeling",
                "company_name": "Scale AI",
                "source_name": "VentureBeat",
                "source_url": "https://example.com/3",
            },
        ]

        print("=" * 50)
        print("测试消息体 JSON：")
        print("=" * 50)
        print(json.dumps(_build_markdown_message(test_items), ensure_ascii=False, indent=2))

        print("\n" + "=" * 50)
        print("推送测试...")
        print("=" * 50)
        success = push_relevant_items(test_items)
        print(f"推送结果：{'成功' if success else '失败'}")
