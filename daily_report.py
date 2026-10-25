"""
=========================================================
每日日报模块（daily_report.py）
=========================================================
职责：把当日（上海时区）相关新闻按领域（AI大模型/数据标注/具身智能）
     汇总成日报：存库（reports 表）+ 钉钉推送（摘要消息 + 完整日报链接）。
=========================================================
设计：
  - 当日素材：query_relevant_by_shanghai_date（混存偏移先 SQL 粗筛再 timeutil 精筛）
  - 跨时间联想（核心）：不喂 N 天原始新闻（会撑爆上下文），而是
      ① 按今日涉及公司检索历史相关新闻（每公司前 5 条、总量封顶 25）
      ② 回放该领域最近 7 期日报的摘要 + 趋势观察
    两者全走 SQL，零 LLM 成本，窗口 = 整个库龄且随运行自然变长。
  - LLM 生成结构化 JSON（五块：summary/events/impact/strategy/trend），
    复用 llm_processor 的 _extract_json 解析风格
  - 钉钉：摘要消息 + actionCard「📖 查看完整日报」按钮 → /reports?date=...
    钉钉不支持发附件，长内容一律"摘要 + 链接"落地
  - 无当日新闻的领域跳过（data_labeling 大概率缺席）；同日期同领域已有日报则跳过

用法：
  python daily_report.py                           # 今天，生成 + 推送
  python daily_report.py --date 2026-08-10        # 指定日期
  python daily_report.py --no-push                # 只生成不推送（验证用）
  python daily_report.py --force                  # 已有日报也强制重写
=========================================================
"""

import json
import logging
import sys
import threading
import time

from config import Config
from database import (
    insert_report, report_exists, query_relevant_by_shanghai_date,
    query_historical_by_companies, get_latest_reports,
)
from notifier import push_report_markdown, CATEGORY_LABELS, DINGTALK_KEYWORD
from timeutil import shanghai_now

logger = logging.getLogger("daily_report")

# 领域顺序（输出/推送按此排），data_labeling 无当日新闻时会自动跳过
CATEGORY_ORDER = ("ai_model", "data_labeling", "embodied_ai")
# 当日涉及公司最多取多少家（历史检索按公司维度，控量控成本）
MAX_COMPANIES = 10
# 历史检索：每公司前 N 条 / 总量封顶
HISTORY_PER_COMPANY = 5
HISTORY_CAP = 25
# 往期日报回放篇数
PREV_REPORTS_LIMIT = 7
# 钉钉消息正文上限（留一点余量给 actionCard 按钮）
PUSH_TEXT_LIMIT = 3800
# LLM 日报结果不可用时的外层重试次数（_call_report_llm 内部已对 JSON 解析失败重试，
# 这里补上"解析成功但内容为空/结构不符"的偶发失败，二者叠加最多约 6 次 API 调用）
REPORT_LLM_ATTEMPTS = 2


# ---------- 文本块组装 ----------

def _format_dt(value) -> str:
    from timeutil import format_minute_shanghai
    return format_minute_shanghai(value)


def _split_companies(items: list) -> list:
    """从当日条目收集公司名（顿号拆分、去重），封顶 MAX_COMPANIES。"""
    seen, out = set(), []
    for it in items:
        for name in (it.get("company_name") or "").split("、"):
            name = name.strip()
            if name and name not in seen:
                seen.add(name)
                out.append(name)
            if len(out) >= MAX_COMPANIES:
                return out
    return out


def _build_news_block(items: list) -> str:
    lines = []
    for it in items:
        title = (it.get("ai_title") or it.get("title") or "").strip()
        source = (it.get("source_name") or "").strip()
        pub = _format_dt(it.get("published_at"))
        summary = (it.get("ai_summary") or "").strip()
        lines.append(f"【{title}】（{source} · {pub}）\n{summary}")
    return "\n\n".join(lines)


def _build_history_block(items: list) -> str:
    lines = []
    for it in items:
        title = (it.get("ai_title") or it.get("title") or "").strip()
        company = (it.get("company_name") or "").strip()
        pub = _format_dt(it.get("published_at"))
        summary = (it.get("ai_summary") or "").strip()
        lines.append(f"【{company}】{title}（{pub}）\n{summary}")
    return "\n\n".join(lines) if lines else "（暂无历史上下文）"


def _build_prev_reports_block(reports: list) -> str:
    lines = []
    for r in reports:
        lines.append(f"【{r['report_date']}】{r.get('title') or ''}\n摘要：{r.get('summary') or ''}\n趋势：{r.get('trend') or ''}")
    return "\n\n".join(lines) if lines else "（暂无往期日报）"


# ---------- LLM 调用 ----------

def _call_report_llm(system_prompt: str, user_prompt: str, max_tokens: int = 2500, retries: int = 2):
    """调用 DeepSeek 生成日报 JSON；解析成功返回 dict，失败返回 None。"""
    from llm_processor import _get_client, _extract_json
    import cost_tracker
    client = _get_client()
    if client is None:
        logger.error("DeepSeek API Key 未配置，无法生成日报")
        return None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=Config.DEEPSEEK_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.3,
                max_tokens=max_tokens,
            )
            # 记账：日报生成通常 2500 token 级，按实际用量累计当日成本
            cost_tracker.record_usage(resp.usage)
            reply = resp.choices[0].message.content or ""
            text = _extract_json(reply)
            if text:
                return json.loads(text)
            logger.warning(f"日报 JSON 解析失败，重试第 {attempt + 1} 次：{reply[:120]}")
            time.sleep(1)
        except Exception as e:
            err = str(e)
            if "429" in err or "rate_limit" in err.lower():
                logger.warning("日报 API 速率限制，等待 10 秒重试...")
                time.sleep(10)
            elif "timeout" in err.lower():
                logger.warning("日报 API 超时，等待 2 秒重试...")
                time.sleep(2)
            else:
                logger.error(f"日报 API 调用失败：{err[:120]}")
                time.sleep(2)
    return None


def _norm_points(val) -> list:
    """
    把 impact/strategy/trend 规范化为要点数组 [{"point": str, "detail": str}]。

    学习注释：
      卡片式分点（2026-08-12）：LLM 新格式输出要点数组；旧日报在库里是纯文本段落，
      这里统一转成单点 [{point:"", detail:原文}]，保证新旧数据前端都能渲染。
    """
    if isinstance(val, list):
        out = []
        for it in val:
            if not isinstance(it, dict):
                continue
            point = str(it.get("point") or "").strip()
            detail = str(it.get("detail") or "").strip()
            # point 或 detail 至少一个非空才保留（都空是无效要点）
            if point or detail:
                out.append({"point": point, "detail": detail})
        return out
    if isinstance(val, str):
        text = val.strip()
        if text:
            return [{"point": "", "detail": text}]
    return []


def _normalize_report(data) -> dict:
    """校验并规范化日报 JSON，字段缺失时安全兜底。"""
    if not isinstance(data, dict):
        return None
    title = str(data.get("title") or "").strip() or "AI 行业动态日报"
    summary = str(data.get("summary") or "").strip()
    events = data.get("events") or []
    if not isinstance(events, list):
        events = []
    ev = []
    for e in events:
        if isinstance(e, dict):
            title_e = str(e.get("title") or "").strip()
            impact = str(e.get("impact") or "").strip()
            if title_e:
                ev.append({"title": title_e, "impact": impact})
    impact_points = _norm_points(data.get("impact"))
    strategy_points = _norm_points(data.get("strategy"))
    trend_points = _norm_points(data.get("trend"))
    # 完全空白的生成结果视为失败
    if not summary and not ev and not impact_points and not strategy_points and not trend_points:
        return None
    return {
        "title": title,
        "summary": summary,
        "events": ev,
        "impact": impact_points,
        "strategy": strategy_points,
        "trend": trend_points,
    }


# ---------- 主流程 ----------

# 进程内互斥：手动双击 / 手动与 20:30 自动任务并发触发时先到先得，
# 配合按领域锁（report_exists + UNIQUE(report_date, category)）杜绝同领域双生成、双推送。
_report_lock = threading.Lock()


def generate_daily_report(report_date: str = None, force: bool = False, push: bool = True) -> dict:
    """
    生成并推送某天的日报。

    参数：
      report_date: 'YYYY-MM-DD'（上海日期）；默认今天
      force:       True 时跳过"已有日报"检查，强制重写
      push:        True 时钉钉推送摘要消息（默认真）

    按领域锁定：每领域当天至多一篇（reports 表 UNIQUE(report_date, category)
    兜底 + report_exists 复查）。当天已生成的领域自动跳过，只补齐尚未生成的
    （如某领域上次生成失败，重复调用会重试它，不会被"当天已生成过"锁死）。
    force=True 忽略所有已有日报，强制重写全部领域。
    """
    target = report_date or shanghai_now().strftime("%Y-%m-%d")
    result = {"date": target, "reports": [], "skipped": []}

    with _report_lock:
        return _generate_impl(target, force, push, result)


def report_missing_categories(report_date: str) -> list:
    """
    当日有相关新闻、但尚无日报的领域列表（按 CATEGORY_ORDER 顺序）。

    用于判断"当天日报是否已补全"：
      空列表 = 所有有新闻的领域都已有日报（当天已完整生成）；
      非空   = 有领域缺失（生成失败/未生成），可重试补齐。
    """
    items = query_relevant_by_shanghai_date(report_date)
    cats_with_news = {it.get("category") or "ai_model" for it in items}
    return [c for c in CATEGORY_ORDER if c in cats_with_news and not report_exists(report_date, c)]


def _generate_impl(target: str, force: bool, push: bool, result: dict) -> dict:
    """generate_daily_report 的正文（在 _report_lock 内串行执行）。"""

    # ---- 0. 每日成本上限门禁（超限 → 今日自动暂停，次日恢复） ----
    import cost_tracker
    if cost_tracker.is_blocked():
        logger.warning("今日 LLM 成本已达上限，跳过日报生成（次日自动恢复）")
        result["skipped"] = "今日LLM成本已达上限"
        return result

    # ---- 1. 排空未处理队列（确保当日条目已 LLM 处理完）----
    from llm_processor import process_unprocessed_items
    try:
        processed = process_unprocessed_items(max_items=100)
        if processed:
            logger.info(f"日报前排空队列：处理了 {processed} 条")
    except Exception as e:
        logger.warning(f"日报前排空队列失败（继续尝试生成）：{e}")

    # ---- 2. 取当日相关条目并分组 ----
    items = query_relevant_by_shanghai_date(target)
    logger.info(f"[日报] {target} 相关条目 {len(items)} 条")
    if not items:
        logger.info("当日无相关新闻，不生成日报")
        result["skipped"] = "当日无相关新闻"
        return result

    groups = {}
    for it in items:
        groups.setdefault(it.get("category") or "ai_model", []).append(it)

    # ---- 3. 逐领域生成 ----
    for cat in CATEGORY_ORDER:
        cat_items = groups.get(cat, [])
        if not cat_items:
            logger.info(f"[日报] {target} {cat}：当日无相关新闻，跳过")
            result["skipped"].append(cat)
            continue
        if not force and report_exists(target, cat):
            logger.info(f"[日报] {target} {cat} 已存在，跳过（--force 可重写）")
            result["skipped"].append(cat)
            continue

        report = _generate_one(target, cat, cat_items)
        if report is None:
            logger.error(f"[日报] {target} {cat} 生成失败")
            result["skipped"].append(cat)
            continue
        result["reports"].append(report)

    # ---- 4. 钉钉推送（摘要 + 查看完整日报按钮）----
    if push and result["reports"]:
        _push_daily_summary(target, result["reports"])

    logger.info(f"[日报] {target} 完成：生成 {len(result['reports'])} 份，跳过 {result['skipped']}")
    return result


def _generate_one(target: str, category: str, cat_items: list) -> dict:
    """生成单领域日报并入库，返回 {category, title, ...} 或 None。"""
    from prompts import build_daily_report_prompt
    cat_label = CATEGORY_LABELS.get(category, category)

    # ---- 跨时间联想素材 ----
    companies = _split_companies(cat_items)
    history = query_historical_by_companies(
        companies, target,
        per_company=HISTORY_PER_COMPANY, cap=HISTORY_CAP,
    )
    prev_reports = get_latest_reports(category, target, limit=PREV_REPORTS_LIMIT)

    news_block = _build_news_block(cat_items)
    history_block = _build_history_block(history)
    prev_block = _build_prev_reports_block(prev_reports)

    logger.info(f"[日报] 生成 {target} {cat_label}：素材 {len(cat_items)} 条 / 历史 {len(history)} 条 / 往期 {len(prev_reports)} 期")

    system_prompt, user_prompt = build_daily_report_prompt(
        cat_label, target, news_block, history_block, prev_block,
    )

    # 结果不可用（JSON 解析成功但字段空/结构不符）也重试：
    # 偶发的"能解析但内容空"曾导致整篇日报失败，且被全局当日锁锁死无法补生成。
    norm = None
    for attempt in range(REPORT_LLM_ATTEMPTS):
        data = _call_report_llm(system_prompt, user_prompt)
        norm = _normalize_report(data) if data is not None else None
        if norm is not None:
            break
        logger.warning(f"[日报] 生成 {target} {cat_label} 结果不可用，重试第 {attempt + 1}/{REPORT_LLM_ATTEMPTS} 次")
        time.sleep(2)
    if norm is None:
        return None

    source_ids = [it["id"] for it in cat_items]
    related_ids = [r["id"] for r in prev_reports]
    raw_md = _build_report_markdown(target, category, norm, source_ids)

    report_id = insert_report(
        report_date=target,
        category=category,
        title=norm["title"],
        summary=norm["summary"],
        events=norm["events"],
        impact=norm["impact"],
        strategy=norm["strategy"],
        trend=norm["trend"],
        source_ids=source_ids,
        source_count=len(source_ids),
        related_report_ids=related_ids,
        raw_markdown=raw_md,
    )
    logger.info(f"[日报] 已入库 {target} {cat_label}（id={report_id}）")
    return {
        "category": category,
        "category_label": cat_label,
        "title": norm["title"],
        "summary": norm["summary"],
        "events": norm["events"],
        "impact": norm["impact"],
        "strategy": norm["strategy"],
        "trend": norm["trend"],
    }


# ---------- Markdown 组装 ----------

def _render_points(points: list) -> list:
    """
    把要点数组渲染成 markdown bullet 列表行（含尾部空行）。
    兼容旧格式：单点且 point 为空 → 按纯文本段落落。
    """
    if len(points) == 1 and not points[0].get("point"):
        return [points[0]["detail"], ""]
    out = []
    for p in points:
        point = (p.get("point") or "").strip()
        detail = (p.get("detail") or "").strip()
        if point and detail:
            out.append(f"- **{point}**：{detail}")
        elif point:
            out.append(f"- **{point}**")
        elif detail:
            out.append(f"- {detail}")
    out.append("")
    return out


def _build_report_markdown(target: str, category: str, norm: dict, source_ids: list) -> str:
    cat_label = CATEGORY_LABELS.get(category, category)
    lines = [
        f"# {norm['title']}",
        "",
        f"**日期**：{target}　**领域**：{cat_label}　**素材**：{len(source_ids)} 条",
        "",
        "## 核心结论",
        norm["summary"],
        "",
        "## 重点事件",
        "",
    ]
    for i, e in enumerate(norm["events"], 1):
        lines.append(f"{i}. **{e['title']}**")
        if e.get("impact"):
            lines.append(e["impact"])
        lines.append("")
    lines.append("## 影响分析")
    lines += _render_points(norm["impact"])
    lines.append("## 战略意义")
    lines += _render_points(norm["strategy"])
    lines.append("## 趋势观察（跨时间联想）")
    lines += _render_points(norm["trend"])
    return "\n".join(lines)


def _push_daily_summary(target: str, reports: list) -> bool:
    """
    钉钉推送：标题行 + 各领域「领域名/日报标题/核心结论」+「查看完整日报」按钮。

    格式（用户指定，2026-08-12）：
      📄 AI 行业动态日报 · 2026-08-12
      📂AI大模型
      日报标题
      核心结论（summary 一段话）
      📂具身智能
      …
      [查看完整日报]（actionCard 按钮，钉钉底部）

    学习注释：
      - 换行必须用 \n\n 空行分段：单个 \n 会被钉钉折叠成空格（notifier 实测），
        <br/> 在安卓/鸿蒙端不生效——唯一可靠换行是空行分段。
      - 正文只含核心结论（summary），不推 events；标题行已含关键词
        DINGTALK_KEYWORD（"AI 行业动态"），标题与正文都带，满足钉钉必含关键词要求。
      - 加粗（2026-08-12，用户要求）：除核心结论正文外，标题行/📂领域名/日报标题
        全部加粗 **…**，正文保持纯文本。钉钉关键词校验看的是原始文本，
        "AI 行业动态" 在 **…** 内仍可命中，不受影响。
    """
    parts = [f"**📄 {DINGTALK_KEYWORD}日报 · {target}**"]
    for r in reports:
        parts.append(f"**📂{r['category_label']}**")
        parts.append(f"**{r['title']}**")
        parts.append(r["summary"])

    text = "\n\n".join(parts)
    if len(text) > PUSH_TEXT_LIMIT:
        text = text[:PUSH_TEXT_LIMIT] + "\n\n> …（已截断，完整内容见网页）"

    url = f"{Config.PUBLIC_BASE_URL}/reports?date={target}"
    logger.info(f"[日报] 钉钉推送摘要（{len(reports)} 份），链接 {url}")
    return push_report_markdown(
        text,
        title=f"📄 {DINGTALK_KEYWORD}日报 · {target}",
        button={"title": "查看完整日报", "url": url},
    )


# ---------- CLI 入口 ----------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    # CLI 直跑时也要加载运行时设置（钉钉群 webhook 来自 DB settings 表），
    # 否则 DINGTALK_WEBHOOKS 为空、push_report_markdown 会静默跳过推送。
    from settings import load_runtime_settings
    load_runtime_settings()
    args = sys.argv[1:]
    date = None
    if "--date" in args:
        i = args.index("--date")
        if i + 1 < len(args):
            date = args[i + 1]
    force = "--force" in args
    push = "--no-push" not in args

    r = generate_daily_report(report_date=date, force=force, push=push)
    print(json.dumps(r, ensure_ascii=False, indent=2))
