"""
=========================================================
RAG 问答模块（qa_service.py）
=========================================================
职责：把用户问题转成检索关键词 → 混合检索（新闻 + 日报）
     → 拼带编号的参考资料 → LLM 生成带引用的答案。
=========================================================
设计：
  - SQLite FTS5 trigram 分词实测不匹配 2 字中文词（如"宇树"），
    所以 ≥3 字词走 items_fts MATCH，2 字纯中文词走 items LIKE 兜底。
  - 检索覆盖两个来源：news（items 表）+ reports（日报表），
    分别 top N 后统一编号，供答案里的 [参考资料 N] 引用。
  - 三态兜底：检索为空 → "库内暂无相关信息"；LLM 失败 → 提示稍后重试；
    正常 → answer + references（带标题/日期/来源/链接，前端可点击）。

用法：
  qa_service.ask("DeepSeek 最近发布了什么")
  返回 {"ok": True, "answer": "...", "references": [...]}
=========================================================
"""

import json
import logging
import time

from config import Config
from database import search_items_fts, search_items_like, search_reports_fts
from prompts import QA_KEYWORD_EXTRACT_PROMPT, build_qa_answer_prompt

logger = logging.getLogger("qa_service")

# 问题长度上限（超出直接拒绝，避免无意义长查询）
MAX_QUESTION_LEN = 200
# 关键词抽取上限（LLM 抽取 3-8 个，这里再截一道）
MAX_KEYWORDS = 8
# 新闻 / 日报各自进最终参考资料的条数
NEWS_TOP_N = 8
REPORTS_TOP_N = 4


# ---------- LLM 调用 ----------

def _call_llm(system_prompt: str, user_prompt: str, max_tokens: int = 1200, retries: int = 1):
    """调用 DeepSeek 返回纯文本；失败返回 None。"""
    from llm_processor import _get_client
    client = _get_client()
    if client is None:
        logger.error("DeepSeek API Key 未配置，无法生成问答")
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
            # 问答不计入「今日 LLM 用量」（成本上限只管新闻/日报等自动任务）
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                # 推理模型思考可能耗尽 max_tokens 导致 content 为空，
                # 空串不能当成功，走重试；两次都空则返回 None 表示失败。
                logger.warning(f"问答 LLM 返回空内容（第 {attempt + 1} 次），可能思考消耗完 max_tokens")
                continue
            return text
        except Exception as e:
            logger.warning(f"问答 API 调用失败（第 {attempt + 1} 次）：{str(e)[:120]}")
            time.sleep(1)
    return None


# ---------- 关键词抽取 ----------

def _extract_keywords(question: str) -> list:
    """LLM 抽取 3-8 个检索关键词；失败/解析异常返回空列表。"""
    from llm_processor import _extract_json
    prompt = QA_KEYWORD_EXTRACT_PROMPT.format(question=question)
    reply = _call_llm("你是一个检索关键词抽取器，只输出 JSON。", prompt, max_tokens=8192)
    if not reply:
        return []
    text = _extract_json(reply)
    if not text:
        return []
    try:
        kws = json.loads(text)
    except Exception:
        return []
    if not isinstance(kws, list):
        return []
    seen, out = set(), []
    for k in kws:
        k = str(k).strip()
        if k and k not in seen:
            seen.add(k)
            out.append(k)
    return out[:MAX_KEYWORDS]


# ---------- 混合检索 ----------

def _is_short_cjk(kw: str) -> bool:
    """判断是否 2 字纯中文词（trigram 分词不匹配，必须走 LIKE 兜底）。"""
    return len(kw) == 2 and all('一' <= c <= '鿿' for c in kw)


def _quote_fts(kw: str) -> str:
    """关键词拼进 FTS MATCH 前加引号，防特殊字符破坏语法。"""
    return '"' + kw.replace('"', '""') + '"'


def _retrieve_with_text(keywords: list) -> list:
    """_retrieve 的带正文版本：为每条引用补充 _text 供上下文拼装。"""
    fts_terms = [_quote_fts(k) for k in keywords if not _is_short_cjk(k)]
    like_terms = [k for k in keywords if _is_short_cjk(k)]

    news = []
    if fts_terms:
        news += search_items_fts(" OR ".join(fts_terms), limit=30)
    if like_terms:
        news += search_items_like(like_terms, limit=30)
    seen_ids, items = set(), []
    for n in news:
        rid = n.get("id")
        if rid in seen_ids:
            continue
        seen_ids.add(rid)
        items.append(n)
    items = items[:NEWS_TOP_N]

    reports = []
    if fts_terms:
        reports = search_reports_fts(" OR ".join(fts_terms), limit=REPORTS_TOP_N)

    from timeutil import format_minute_shanghai
    refs = []
    for n in items:
        title = (n.get("ai_title") or n.get("title") or "").strip()
        body = (n.get("ai_summary") or "").strip()
        refs.append({
            "kind": "news",
            "item_id": n.get("id"),
            "title": title,
            "url": n.get("source_url") or "",
            "date": format_minute_shanghai(n.get("published_at")) or "",
            "source": (n.get("source_name") or "").strip(),
            "_text": body,
        })
    for r in reports:
        title = (r.get("title") or "").strip()
        body = (r.get("summary") or "").strip()
        if not body and r.get("events"):
            body = r["events"]
        refs.append({
            "kind": "report",
            "item_id": r.get("id"),
            "title": title,
            "url": f"/reports?date={r.get('report_date', '')}&category={r.get('category', '')}",
            "date": r.get("report_date") or "",
            "source": "日报",
            "_text": body,
        })
    return refs


# ---------- 上下文组装 ----------

def _without_text(refs: list) -> list:
    """去掉引用里的内部字段 _text（只用于拼上下文，不返回给前端）。"""
    return [{k: v for k, v in r.items() if k != "_text"} for r in refs]


def _build_context(refs: list) -> str:
    """把带 _text 的引用列表拼成 LLM 可见的编号参考资料文本。

    refs 里的 date 已在 _retrieve_with_text 统一格式化为分钟级 +08:00（news）
    或 YYYY-MM-DD（report），此处直接拼接，不要再做二次时区转换。
    """
    blocks = []
    for i, r in enumerate(refs, 1):
        when = r.get("date") or ""
        head = f"[参考资料 {i}] {r['title']}（{when} · {r['source']}）"
        blocks.append(f"{head}\n{r.get('_text') or ''}")
    return "\n\n".join(blocks)


# ---------- 主入口 ----------

def ask(question: str) -> dict:
    """回答一个问题，返回 {ok, answer, references, error?}。"""
    question = (question or "").strip()
    if not question:
        return {"ok": False, "error": "问题不能为空"}
    if len(question) > MAX_QUESTION_LEN:
        return {"ok": False, "error": f"问题过长（最多 {MAX_QUESTION_LEN} 字）"}

    # 1. 抽取检索关键词（问答不计入「今日 LLM 用量」，不受成本上限门禁）
    keywords = _extract_keywords(question)
    if not keywords:
        # 抽取失败 = LLM 没给出可用关键词（如高推理强度模型思考耗尽预算）。
        # 此时检索与回答都依赖 LLM，直接明示服务暂不可用，而不是误导成「库内暂无」。
        logger.warning("关键词抽取失败（LLM 未返回可用关键词），问答暂不可用")
        return {"ok": False, "error": "AI 服务暂时无法处理该问题，请稍后重试。"}

    # 2. 混合检索
    refs = _retrieve_with_text(keywords)
    if not refs:
        return {"ok": True, "answer": "库内暂无相关信息，换个问法试试。", "references": []}

    # 3. 拼上下文并生成答案
    context = _build_context(refs)
    sys_prompt, user_prompt = build_qa_answer_prompt(question, context)
    reply = _call_llm(sys_prompt, user_prompt, max_tokens=8192)
    if not reply:
        return {"ok": True, "answer": "生成回答失败，请稍后重试。", "references": _without_text(refs)}

    return {"ok": True, "answer": reply, "references": _without_text(refs)}


# ---------- 测试入口 ----------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    from settings import load_runtime_settings
    load_runtime_settings()

    import sys
    q = " ".join(sys.argv[1:]) or "DeepSeek 最近发布了什么？"
    result = ask(q)
    print("=" * 50)
    print(f"问题：{q}")
    print(f"ok: {result.get('ok')}")
    if result.get("error"):
        print(f"错误：{result['error']}")
    print(f"引用 {len(result.get('references', []))} 条：")
    for i, r in enumerate(result.get("references", []), 1):
        print(f"  [{i}] [{r['kind']}] {r['title']}（{r['date']} · {r['source']}）")
    print("-" * 50)
    print(result.get("answer", ""))
