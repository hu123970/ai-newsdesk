"""
=========================================================
采集器模块（collector.py）— Stage 2 实现
=========================================================
职责：从 RSS/网页获取最新内容，解析为 RawItem 后存入数据库。
=========================================================
学习注释：
  采集器的工作流程：
  1. 加载 sources.json 获取所有信息源配置
  2. 遍历每个信息源，根据类型（rss/webpage）调用不同的解析方式
  3. 将解析出的内容插入数据库（URL 去重自动生效）
  4. 记录采集统计信息，方便排查问题

  feedparser：专门用来解析 RSS/Atom 的 Python 库，
  它会自动处理各种 RSS 格式的差异（RSS 2.0、Atom 等），
  返回统一的数据结构，省去手动解析 XML 的麻烦。
=========================================================
"""

import json
import logging
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List, Optional
from urllib.error import URLError

import feedparser
import httpx

from config import Config
from database import insert_item
from models import RawItem, SourceConfig
from prompts import quick_filter

# ---------- 日志配置 ----------
# 采集过程的日志很重要，方便排查"为什么某个源采不到数据"
logger = logging.getLogger("collector")


# ---------- 信息源加载 ----------

def load_sources() -> List[SourceConfig]:
    """
    从 sources.json 加载信息源配置。

    返回：
      SourceConfig 对象列表

    学习注释：
      如果 sources.json 格式有问题（比如少了一个逗号），
      JSON 解析就会失败。这里捕获异常并打印友好提示。
    """
    try:
        with open(Config.SOURCES_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.error(f"找不到信息源配置文件：{Config.SOURCES_PATH}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"信息源配置文件 JSON 格式错误：{e}")
        return []

    sources = []
    for item in data.get("sources", []):
        sources.append(SourceConfig(
            name=item.get("name", "未命名来源"),
            source_type=item.get("source_type", "rss"),
            url=item.get("url", ""),
            region=item.get("region", "overseas"),
            category=item.get("category", "ai_model"),
        ))
    return sources


# ---------- RSS 解析 ----------

def parse_feed(source: SourceConfig, timeout: int = 15) -> List[RawItem]:
    """
    解析单个 RSS 信息源。

    参数：
      source: 信息源配置对象
      timeout: 网络请求超时时间（秒），默认 15 秒

    返回：
      RawItem 列表（解析出的原始内容）

    学习注释：
      用 httpx 先发 HTTP 请求拿到 RSS 内容，再交给 feedparser 解析。
      这样做的好处是可以控制超时时间（feedparser 自带的不支持超时），
      避免被某个不可达的源卡住整个采集流程。
    """
    items = []
    try:
        # 用 httpx 发请求，设置超时防止卡死
        # verify=False 跳过 SSL 证书验证（部分国内源证书配置有问题）
        with httpx.Client(timeout=timeout, follow_redirects=True, verify=False,
                          headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36"}) as client:
            response = client.get(source.url)
            response.raise_for_status()  # 非 200 状态码会抛出异常
            content = response.text
    except httpx.TimeoutException:
        logger.warning(f"[{source.name}] 请求超时（{timeout}秒），跳过")
        return items
    except httpx.HTTPStatusError as e:
        logger.warning(f"[{source.name}] HTTP 错误 {e.response.status_code}，跳过")
        return items
    except Exception as e:
        logger.warning(f"[{source.name}] 网络请求失败：{e}")
        return items

    # 用 feedparser 解析 RSS 内容
    try:
        feed = feedparser.parse(content)
    except Exception as e:
        logger.warning(f"[{source.name}] RSS 解析失败：{e}")
        return items

    # 检查 feed 是否解析成功
    # bozo 是 feedparser 的错误标记，1 表示有解析问题
    if feed.bozo and not feed.entries:
        # 有严重错误且没有条目，跳过
        logger.warning(f"[{source.name}] RSS 源无法访问或格式错误")
        return items

    for entry in feed.entries:
        # ---- 提取标题 ----
        title = (entry.get("title") or "").strip()
        if not title:
            # 没有标题的条目直接跳过
            continue

        # ---- 提取链接 ----
        url = (entry.get("link") or "").strip()
        if not url:
            # 没有链接的条目无法跳转原文，也跳过
            continue

        # ---- 提取发布时间 ----
        # RSS 中的时间格式多样，按优先级尝试多种方式解析
        published_at = _parse_published_time(entry)

        # ---- 提取内容 ----
        # 优先取 summary（大多数 RSS 都有），没有就用 content 或 description
        content_text = ""
        if entry.get("summary"):
            content_text = entry.get("summary", "")
        elif entry.get("content"):
            # content 字段可能是列表（Atom 格式）
            content_list = entry.get("content", [])
            if isinstance(content_list, list) and len(content_list) > 0:
                content_text = content_list[0].get("value", "")
            else:
                content_text = str(content_list)
        elif entry.get("description"):
            content_text = entry.get("description", "")

        # 清理 HTML 标签（简单粗暴版）
        # 后续 LLM 处理时会做更精细的清洗，这里先去掉基本的 HTML
        content_text = _strip_html(content_text)

        # ---- 构造 RawItem ----
        items.append(RawItem(
            title=title,
            url=url,
            source_name=source.name,
            published_at=published_at,
            content_text=content_text[:2000],  # 截断超长内容，节约存储空间
            is_rss=True,
        ))

    return items


def _parse_published_time(entry) -> datetime:
    """
    从 feed entry 中解析发布时间。

    学习注释：
      RSS 没有统一的时间格式。不同的 RSS 源可能用不同的格式：
      - RFC 2822（如 "Mon, 28 Jul 2025 10:30:00 +0000"）
      - ISO 8601（如 "2025-07-28T10:30:00+00:00"）
      - 还有纯日期格式
      这里按优先级尝试多种解析方式，都失败就用当前时间兜底。
    """
    # 方式一：用 email 库解析 RFC 2822 格式
    published_str = entry.get("published", "")
    if published_str:
        try:
            return parsedate_to_datetime(published_str)
        except (ValueError, TypeError):
            pass

    # 方式二：用 feedparser 自带的解析结果
    if entry.get("published_parsed"):
        try:
            # published_parsed 是 struct_time 元组，feedparser 统一归一化成 UTC。
            # ⚠️ 必须标记 tzinfo=UTC，否则会存成"不带时区的 UTC 时间"，
            #    前端相对时间会把它当本地时间，差 8 小时（36氪/The Verge 曾踩坑）。
            struct_time = entry.published_parsed
            return datetime(*struct_time[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    # 方式三：用 updated_parsed（有些 Atom 格式用这个字段）
    if entry.get("updated_parsed"):
        try:
            struct_time = entry.updated_parsed
            return datetime(*struct_time[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            pass

    # 兜底：返回当前时间（统一用 UTC，保证所有路径产出 aware datetime）
    return datetime.now(timezone.utc)


def _strip_html(text: str) -> str:
    """
    去除 HTML 标签，保留纯文本。

    学习注释：
      用正则去掉 <...> 形式的标签，并处理常见的 HTML 实体。
      这是简化版的处理，正式的 HTML 解析应该用 BeautifulSoup，
      但这里只是预处理，后续 LLM 还会再处理，所以不用太精细。
    """
    import re
    # 去掉所有 HTML 标签
    text = re.sub(r"<[^>]+>", "", text)
    # 处理常见的 HTML 实体
    text = text.replace("&amp;", "&")
    text = text.replace("&lt;", "<")
    text = text.replace("&gt;", ">")
    text = text.replace("&quot;", "\"")
    text = text.replace("&#39;", "'")
    text = text.replace("&nbsp;", " ")
    # 合并多余空白
    text = re.sub(r"\s+", " ", text)
    return text.strip()


# ---------- 网页抓取（扩展预留） ----------

def parse_webpage(source: SourceConfig) -> List[RawItem]:
    """
    从普通网页抓取内容（当前所有信息源都是 RSS，这里预留接口）。

    学习注释：
      网页抓取比 RSS 复杂得多，因为每个网站的 HTML 结构不同，
      需要针对每个网站写解析规则（CSS 选择器 / XPath）。
      当前项目所有源都是 RSS，所以这个函数留空。
      后续如果需要抓取某个没有 RSS 的网站，再在这里实现。
    """
    logger.warning(f"[{source.name}] 网页抓取功能尚未实现，跳过")
    return []


# ---------- 主入口 ----------

def collect_all_sources() -> int:
    """
    采集所有信息源的最新内容。

    工作流程：
      1. 加载所有信息源配置
      2. 逐个解析每个信息源
      3. 解析出的内容插入数据库（URL 去重自动生效）
      4. 打印统计结果

    返回：
      本次采集到的新条目数量

    学习注释：
      这个函数是采集器的"总调度"。
      它不关心具体是 RSS 还是网页，根据 source_type 分发给对应的函数。
      所有的网络错误、解析错误都在子函数中处理，不会中断整个流程。
    """
    sources = load_sources()
    if not sources:
        logger.warning("没有可用的信息源，请检查 sources.json")
        return 0

    total_new = 0
    total_skipped_old = 0
    total_skipped_keyword = 0
    # 统一按 UTC 比较：_parse_published_time 现在返回 aware datetime，
    # 不能再用本地 naive now 去减，否则国内外源的时区差异会算错年龄。
    cutoff = datetime.now(timezone.utc) - timedelta(days=Config.MAX_ITEM_AGE_DAYS)
    cutoff_naive = cutoff.replace(tzinfo=None)
    logger.info(f"开始采集 {len(sources)} 个信息源（仅保留 {Config.MAX_ITEM_AGE_DAYS} 天内的内容）...")

    for i, source in enumerate(sources, 1):
        logger.info(f"[{i}/{len(sources)}] 正在采集：{source.name}")

        # ---- 第一步：解析信息源 ----
        if source.source_type == "rss":
            raw_items = parse_feed(source)
        elif source.source_type == "webpage":
            raw_items = parse_webpage(source)
        else:
            logger.warning(f"未知的信息源类型：{source.source_type}，跳过 {source.name}")
            continue

        if not raw_items:
            logger.info(f"  → {source.name}：未获取到新内容")
            continue

        # ---- 第二步：逐条插入数据库 ----
        new_count = 0
        skipped_old = 0
        skipped_keyword = 0
        for item in raw_items:
            # 过滤掉 N 天前的旧内容（防止 RSS 源返回历史灌水）
            if item.published_at.astimezone(timezone.utc).replace(tzinfo=None) < cutoff_naive:
                skipped_old += 1
                continue
            # 关键词预筛选：标题/内容不涉及 AI 或目标公司的直接丢弃
            if not quick_filter(item.title, item.content_text):
                skipped_keyword += 1
                continue
            try:
                result = insert_item(
                    source_url=item.url,
                    source_name=item.source_name,
                    title=item.title,
                    content_text=item.content_text,
                    published_at=item.published_at,
                    region=source.region,
                )
                if result is not None:
                    new_count += 1  # 成功插入新条目
            except Exception as e:
                logger.error(f"  → 插入失败 [{item.title[:30]}...]：{e}")

        total_new += new_count
        total_skipped_old += skipped_old
        total_skipped_keyword += skipped_keyword
        logger.info(f"  → {source.name}：获取 {len(raw_items)} 条，新增 {new_count} 条，关键词过滤 {skipped_keyword} 条，跳过旧数据 {skipped_old} 条")

    logger.info(f"采集完成！共新增 {total_new} 条，关键词过滤 {total_skipped_keyword} 条，跳过旧数据 {total_skipped_old} 条")
    return total_new


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python collector.py 可以测试采集功能。

    学习注释：
      独立运行采集器可以方便调试，不用启动整个 Web 服务器。
      日志级别设为 INFO，可以看到每个信息源的采集结果。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    # 确保数据库已初始化
    from database import init_database
    init_database()

    print("=" * 50)
    print("开始测试采集功能...")
    print("=" * 50)
    new_count = collect_all_sources()
    print("=" * 50)
    print(f"采集测试完成！新增 {new_count} 条内容。")
    print(f"数据库位置：{Config.DATABASE_PATH}")
    print("=" * 50)
