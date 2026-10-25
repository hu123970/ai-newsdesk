"""
=========================================================
应用主入口（main.py）
=========================================================
职责：FastAPI 应用启动、路由注册、页面渲染。
=========================================================
学习注释：
  这是项目的"总调度"，负责：
  1. 启动时初始化数据库
  2. 定义所有页面路由（URL 路径对应的处理函数）
  3. 注册定时任务（Stage 2 加）
  这里不写采集逻辑，不写 LLM 调用，只做"组装"。
=========================================================
"""

import json
import re
import logging
from datetime import datetime
from html import escape
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from config import Config
from database import (
    init_database, query_items, query_distinct_companies, get_total_count,
    get_unprocessed_source_stats, query_reports, get_reports_total, report_exists,
    get_news_total, get_news_today, get_ten_sources, get_category_count,
    get_region_count, get_daily_count, get_ten_company
)
from models import FilterOptions
from scheduler import start_scheduler, collect_now, is_scheduler_paused, toggle_scheduler
from llm_processor import process_unprocessed_items
from daily_report import generate_daily_report, report_missing_categories
from timeutil import shanghai_now
from settings import (
    load_runtime_settings, mask_key, save_deepseek_key, save_webhooks, save_target_companies,
    save_llm_provider, set_active_provider, get_llm_providers,
)
from providers import PROVIDERS
from logutil import setup_logging
import auth
import cost_tracker
import prompts

logger = logging.getLogger("main")

# ---------- 第一步：创建 FastAPI 应用 ----------
app = FastAPI(
    title=Config.APP_TITLE,
    description="AI 行业动态信息聚合平台",
    version="0.1.0",
)

# ---------- 第二步：挂载静态文件和模板 ----------
# 静态文件（如 CSS、JS）通过 /static/ 路径访问
# 例：/static/style.css 对应 static/style.css 文件
static_path = Path(__file__).parent / "static"
if static_path.exists():
    app.mount("/static", StaticFiles(directory=str(static_path)), name="static")

# Jinja2 模板引擎
# 模板文件（.html）放在 templates/ 目录下
templates_path = Path(__file__).parent / "templates"
templates = Jinja2Templates(directory=str(templates_path))


# ---------- 关键词高亮过滤器 ----------

def _highlight(text, keyword):
    """
    Jinja 过滤器：把文本里命中的关键词包上 <mark class="hl">。

    学习注释：
      任何分支都先 html.escape 再返回（配合模板里的 | safe 使用，
      否则 | safe 会放行未转义文本造成 XSS）。关键词 re.escape +
      re.IGNORECASE：ASCII 大小写不敏感（'v4' 命中 'V4'），中文精确匹配。
      无关键词时返回转义后的原文，不影响无筛选时的正常渲染。
    """
    if not text:
        return ""
    escaped = escape(str(text))
    if not keyword:
        return escaped
    kw = escape(keyword)  # 关键词里若含 <>& 等字符，按转义后的形态匹配
    try:
        pattern = re.compile(re.escape(kw), re.IGNORECASE)
    except re.error:
        return escaped
    return pattern.sub(lambda m: f'<mark class="hl">{m.group(0)}</mark>', escaped)


templates.env.filters["highlight"] = _highlight


# ---------- 全站登录守卫（认证中间件） ----------
# 放行 /login、/static/*、/api/auth/request_reset|reset；其余一律要登录。
# 未登录时：HTMX → 401+HX-Redirect，/api/* → 401 JSON，页面 → 302 登录页。
# 注意：必须在路由注册之前挂，确保所有请求先过守卫。

@app.middleware("http")
async def auth_guard(request, call_next):
    return await auth.auth_middleware(request, call_next)


# ---------- 第三步：启动事件 ----------

@app.on_event("startup")
async def startup_event():
    """
    应用启动时自动执行：
    1. 初始化数据库（建表）
    2. 加载运行时配置（API Key / 钉钉群 / 监控公司，首次用 .env 种子化）
    3. 确保默认登录密码已初始化（首次启动写 12345678）
    4. 启动定时采集调度器
    """
    # 业务日志落盘（logs/app.log），幂等配置
    log_file = setup_logging()
    logging.getLogger("main").info(f"日志已写入 {log_file}")
    print(f"[启动] {Config.APP_TITLE} v0.1.0")
    init_database()
    # 加载运行时配置（API Key / 钉钉群 / 监控公司，首次用 .env 种子化）
    load_runtime_settings()
    # 首次启动：settings 表无密码哈希时写入默认密码（现有种子分支不会执行）
    auth.ensure_default_password()
    # 双身份：用户身份（staff）默认密码同样首次初始化
    auth.ensure_default_user_password()
    # 启动定时采集任务（Stage 2）
    start_scheduler()
    print(f"[启动] 就绪！访问 http://localhost:{Config.PORT} 打开页面")


# ---------- 第四步：页面路由 ----------


def _parse_date_param(value: str) -> str:
    """
    校验 YYYY-MM-DD 日期参数；空串或非法（如 2026-02-30）返回空串。
    非法日期直接忽略而不是报 500，前端乱填也不影响页面。
    """
    value = (value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
        return value
    except ValueError:
        return ""


@app.get("/", response_class=HTMLResponse)
async def index_page(request: Request):
    """
    首页 —— 显示动态列表。

    学习注释：
      request 对象包含用户的请求信息（URL参数、Cookie等）。
      filters 从 URL 查询参数获取，比如：
        ?category=ai_model&region=overseas
      如果没有传参，就使用默认值（全部显示）。
    """
    # ---- 读取筛选条件 ----
    category = request.query_params.get("category", "")
    region = request.query_params.get("region", "")
    company_name = request.query_params.get("company_name", "")
    keyword = request.query_params.get("keyword", "").strip()
    date_from = _parse_date_param(request.query_params.get("date_from", ""))
    date_to = _parse_date_param(request.query_params.get("date_to", ""))
    # date_from > date_to 时互换，避免"必然查不到"的困惑
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from
    page_str = request.query_params.get("page", "1")

    try:
        page = int(page_str)
    except ValueError:
        page = 1
    if page < 1:
        page = 1

    filters = FilterOptions(
        category=category,
        region=region,
        company_name=company_name,
        keyword=keyword,
        date_from=date_from,
        date_to=date_to,
        page=page,
        page_size=Config.PAGE_SIZE,
    )

    # ---- 查询数据 ----
    items = query_items(filters)   # 动态列表
    total = get_total_count(filters)  # 总条数（分页用）
    companies = query_distinct_companies()  # 所有公司（下拉框用）
    total_pages = max(1, (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE)

    # ---- 渲染页面 ----
    _ups = get_unprocessed_source_stats()
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "app_title": Config.APP_TITLE,
            "active_tab": "news",
            # 当前登录身份（双身份：管理员 / 用户）
            "current_username": getattr(request.state, "username", auth.AUTH_USERNAME),
            "current_role": getattr(request.state, "role", "admin"),
            # 今日 LLM 成本是否已达上限（页面 AI 状态栏显示「今日已暂停」）
            "llm_cost_blocked": cost_tracker.is_blocked(),
            "items": items,              # 动态列表
            "companies": companies,       # 公司列表（供筛选/搜索）
            "current_category": category,
            "current_region": region,
            "current_company": company_name,
            "current_keyword": keyword,
            "current_date_from": date_from,
            "current_date_to": date_to,
            "current_page": page,
            "total_pages": total_pages,
            "total": total,
            # 没有数据时显示空状态提示
            "has_items": len(items) > 0,
            # 是否设置了筛选条件（用于区分"无数据"和"筛不出"）
            "has_filters": bool(category or region or company_name or keyword or date_from or date_to),
            # API Key 是否已配置（用于页面状态指示器）
            "has_api_key": bool(Config.DEEPSEEK_API_KEY),
            # 数据管理面板（Stage 4+）
            "unprocessed_stats": _ups,
            "unprocessed_total": sum(s["count"] for s in _ups),
            # 定时采集开关状态（True=已暂停，显示「开始」按钮）
            "scheduler_paused": is_scheduler_paused(),
        },
    )


# ---------- 第五步：手动触发采集（Stage 2） ----------

@app.post("/collect")
async def manual_collect(request: Request):
    """
    手动触发一次采集（不等待定时任务）。

    学习注释：
      用户在前端点击"手动刷新"按钮时，会发一个 POST 请求到这里。
      收到请求后立即执行采集，然后重定向回首页。
      如果是 HTMX 请求（前端 JS 发的），则返回一段 HTML 片段。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    new_count = collect_now()

    # HTMX 请求：返回简洁的操作反馈
    hx_request = request.headers.get("HX-Request", "")
    if hx_request == "true":
        if new_count > 0:
            msg = f'<span class="text-green-600">🔄 采集完成！新增 {new_count} 条，<a href="/" class="underline">刷新页面</a>查看</span>'
        else:
            msg = '<span class="text-gray-500">🔄 采集完成，暂无新内容</span>'
        return HTMLResponse(f'<div class="feedback-fade">{msg}</div>')

    # 普通请求：重定向回首页
    return RedirectResponse(url="/", status_code=303)


@app.post("/process")
async def manual_process(request: Request):
    """
    手动触发一次 LLM 处理（不等待定时任务）。

    学习注释：
      Stage 3 新增。用户点击"AI 分析"按钮时触发。
      批量处理未分析的条目，每次最多处理 20 条。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    processed = process_unprocessed_items(max_items=20)

    hx_request = request.headers.get("HX-Request", "")
    if hx_request == "true":
        if processed > 0:
            msg = f'<span class="text-green-600">🤖 AI 分析完成！处理了 {processed} 条，<a href="/" class="underline">刷新页面</a>查看</span>'
        elif not Config.DEEPSEEK_API_KEY:
            msg = '<span class="text-yellow-600">⚠️ 请先配置 DEEPSEEK_API_KEY</span>'
        else:
            msg = '<span class="text-gray-500">🤖 暂无待处理的条目</span>'
        return HTMLResponse(f'<div class="feedback-fade">{msg}</div>')

    return RedirectResponse(url="/", status_code=303)


@app.post("/api/scheduler/toggle")
async def api_scheduler_toggle(request: Request):
    """
    切换定时采集的暂停 / 恢复，返回新的按钮片段（HTMX 就地替换）。

    学习注释：
      - 运行中点击「暂停」：停止所有定时任务触发（定时采集 + LLM 处理），
        按钮变为「▶ 开始」。
      - 暂停中点「开始」：恢复按预设时间节点的定时采集，按钮变回「⏸ 暂停」。
      - 手动「🔄 采集」按钮不受影响，暂停时也可手动采集一次。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    paused = toggle_scheduler()
    return HTMLResponse(_scheduler_button_html(paused))


def _scheduler_button_html(paused: bool) -> str:
    """根据暂停状态生成开始/暂停按钮片段（与模板中的写法保持一致）。"""
    if paused:
        return (
            '<button hx-post="/api/scheduler/toggle" hx-target="#scheduler-btn-wrap" hx-swap="innerHTML" '
            'class="btn btn-primary px-2.5 py-1 text-xs rounded-md flex items-center justify-center gap-1 w-[4.25rem]">▶ 开始</button>'
        )
    return (
        '<button hx-post="/api/scheduler/toggle" hx-target="#scheduler-btn-wrap" hx-swap="innerHTML" '
        'class="btn btn-secondary px-2.5 py-1 text-xs rounded-md flex items-center justify-center gap-1 w-[4.25rem]">⏸ 暂停</button>'
    )


# ---------- 手动生成日报（日报页「生成日报」按钮） ----------

def _report_button_html(disabled: bool = False) -> str:
    """日报页顶栏「生成日报」按钮片段（尺寸/样式对齐「采集/暂停」键，就地换入 #report-btn-wrap）。"""
    btn_cls = "btn btn-primary px-2.5 py-1 text-xs rounded-md flex items-center justify-center gap-1 min-w-[5.5rem] whitespace-nowrap"
    if disabled:
        return (
            f'<button disabled class="btn btn-secondary px-2.5 py-1 text-xs rounded-md flex items-center '
            f'justify-center gap-1 opacity-60 cursor-not-allowed whitespace-nowrap">✅ 今日已生成</button>'
        )
    return (
        f'<button hx-post="/api/reports/generate" hx-target="#report-btn-wrap" hx-swap="innerHTML" '
        f'hx-indicator="#report-spinner" hx-disabled-elt="this" '
        f'hx-on::after-request="if(event.detail.successful){{setTimeout(function(){{window.location.reload()}},500)}}" '
        f'class="{btn_cls}">'
        f'<span id="report-spinner" class="htmx-indicator">'
        f'<svg class="animate-spin h-3 w-3" viewBox="0 0 24 24"><circle class="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" stroke-width="4" fill="none"/><path class="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8V0C5.373 0 0 5.373 0 12h4z"/></svg>'
        f'</span>📄 生成日报</button>'
    )


@app.post("/api/reports/generate")
def generate_reports_now(request: Request):
    """
    手动生成当日各领域日报并钉钉推送（群发全部已配置群）。

    按领域锁定：每领域当天至多一篇（reports 表 UNIQUE + report_exists 兜底）。
    重复点击只补齐当天尚未生成、但有新闻的领域（例如上次生成失败的领域），
    已存在的领域不会重复生成、也不会重复推送。用普通 def（FastAPI 丢线程池执行），
    LLM 长耗时不会卡事件循环。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    target = shanghai_now().strftime("%Y-%m-%d")
    hx_request = request.headers.get("HX-Request", "") == "true"

    def _resp(msg: str, color: str, disabled: bool):
        if not hx_request:
            return RedirectResponse(url=f"/reports?date={target}", status_code=303)
        return HTMLResponse(
            _report_button_html(disabled)
            + f'<div id="action-feedback" hx-swap-oob="true"><div class="feedback-fade"><span class="{color}">{msg}</span></div></div>'
        )

    # 当天日报已补全（所有有新闻的领域都已有日报）→ 无需再调 LLM
    if not report_missing_categories(target) and report_exists(target):
        logger.info(f"[日报] 手动生成：{target} 各领域日报均已生成，无需重复")
        return _resp("✅ 今日各领域日报均已生成，无需重复", "text-green-600", disabled=True)

    result = generate_daily_report(push=True)
    n = len(result.get("reports") or [])

    if n > 0:
        complete = not report_missing_categories(target)
        msg = (f'✅ 日报生成完成：{n} 份，已推送钉钉，'
               f'<a href="/reports?date={target}" class="underline">查看今日日报</a>')
        if not complete:
            msg += '（部分领域仍缺，可再次点击重试）'
        return _resp(msg, "text-green-600", disabled=complete)

    # 本轮没有新生成：当日无新闻，或成本上限暂停，或全部领域生成失败
    if isinstance(result.get("skipped"), str) and "成本" in str(result.get("skipped")):
        return _resp("⚠️ 今日 LLM 成本已达上限，已自动暂停（次日恢复）", "text-yellow-600", disabled=False)
    if isinstance(result.get("skipped"), str) and "无相关新闻" in str(result.get("skipped")):
        return _resp("⚠️ 当日无相关新闻，未生成日报", "text-yellow-600", disabled=False)
    return _resp("⚠️ 日报生成失败，请稍后重试", "text-yellow-600", disabled=False)

# ---------- 新闻统计界面 ----------

@app.get("/api/stats")
async def api_get_stats(request: Request):
    """
    获取新闻库的部分统计数据，以json形式在前端展示出来
    """
    today = shanghai_now().strftime("%Y-%m-%d")

    category = get_category_count()
    from notifier import CATEGORY_LABELS
    for c in category:
        c['label'] = CATEGORY_LABELS.get(c['name'])

    region = get_region_count()
    REGION_LABELS = {"china": "国内", "overseas": "海外", "unknown": "未知"}
    for r in region:
        r['label'] = REGION_LABELS.get(r['name'])

    return JSONResponse({"ok": True,
                         "total": get_news_total(), 
                         "today_date":today, 
                         "today_total": get_news_today(today),
                         "source": get_ten_sources(),
                         "category": category,
                         "region": region,
                         "daily": get_daily_count(),
                         "company": get_ten_company()})

# ---------- 第六步：设置接口（设置弹窗用） ----------


@app.get("/api/settings")
async def api_get_settings(request: Request):
    """
    读取当前设置（供设置弹窗初始化）。

    返回：
      - llm_provider：当前使用的供应商 id
      - llm_providers：所有供应商展示信息 {pid:{label,masked_key,base_url,model,tip}}
      - llm_cost_today：今日 LLM 已用金额（元）
      - webhooks:[{name,url}]：完整 URL（webhook 是编辑字段，必须完整展示才能改）
      - companies:[{name,region,category,keywords}]
      - has_api_key
    """
    if not _is_admin(request):
        return _admin_forbidden()

    webhooks = Config.DINGTALK_WEBHOOKS or []
    return JSONResponse({
        "llm_provider": getattr(Config, "LLM_ACTIVE_PROVIDER", "deepseek"),
        "llm_providers": get_llm_providers(),
        "llm_cost_today": cost_tracker.get_today_cost(),
        "webhooks": [
            {"name": w.get("name", ""), "url": w.get("url", "")}
            for w in webhooks
        ],
        "companies": prompts.get_current_companies(),
        "has_api_key": bool(Config.DEEPSEEK_API_KEY),
    })


@app.post("/api/settings")
async def api_save_settings(request: Request):
    """
    保存设置（设置弹窗点"保存"）。支持局部更新：
      {llm_provider?, api_key?, base_url?, model?, webhooks?: [...], companies?: [...]}
    供应商配置：api_key 空 = 保持不变；改的是当前选中的供应商，保存后即切换生效。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    if not isinstance(data, dict):
        return JSONResponse({"ok": False, "msg": "❌ 请求体格式错误"}, status_code=400)

    errors = []
    masked_key = None

    # ---- AI 模型（供应商）配置 ----
    if "llm_provider" in data:
        provider = (data.get("llm_provider") or "").strip()
        if provider not in PROVIDERS:
            errors.append(f"未知供应商：{provider}")
        else:
            try:
                # 有 key/base_url/model 字段就先保存该供应商配置，再切换为当前使用
                if any(k in data for k in ("api_key", "base_url", "model")):
                    masked_key = save_llm_provider(
                        provider,
                        api_key=data.get("api_key") or "",
                        base_url=data.get("base_url") or "",
                        model=data.get("model") or "",
                    )
                set_active_provider(provider)
            except ValueError as e:
                errors.append(f"AI 模型：{e}")
    else:
        # 向后兼容：旧前端只发 deepseek_api_key
        try:
            if "deepseek_api_key" in data:
                masked_key = save_deepseek_key(data.get("deepseek_api_key") or "")
        except ValueError as e:
            errors.append(f"API Key：{e}")

    try:
        if "webhooks" in data:
            save_webhooks(data.get("webhooks") or [])
    except ValueError as e:
        errors.append(f"Webhook：{e}")

    try:
        if "companies" in data:
            save_target_companies(data.get("companies") or [])
    except ValueError as e:
        errors.append(f"监控公司：{e}")

    if errors:
        return JSONResponse({"ok": False, "msg": "；".join(errors)}, status_code=400)

    return JSONResponse({
        "ok": True,
        "msg": "✅ 设置已保存",
        "llm_provider": getattr(Config, "LLM_ACTIVE_PROVIDER", "deepseek"),
        "deepseek_api_key_masked": masked_key if masked_key is not None else mask_key(Config.DEEPSEEK_API_KEY),
    })


@app.post("/api/settings/test_key")
async def api_test_key(request: Request):
    """
    测试当前选中供应商的 API 连通性。
    用 body 里的 api_key/base_url/model（不传则用当前值）发一次最小请求验证。
    1-token 的 ping 不计入成本记账（金额可忽略，避免干扰上限统计）。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    api_key = (data.get("api_key") or "").strip() or Config.DEEPSEEK_API_KEY
    base_url = (data.get("base_url") or "").strip() or Config.DEEPSEEK_BASE_URL
    model = (data.get("model") or "").strip() or Config.DEEPSEEK_MODEL
    if not api_key:
        return JSONResponse({"ok": False, "msg": "❌ 未提供 API Key"}, status_code=400)
    if not model:
        return JSONResponse(
            {"ok": False, "msg": "❌ 请先填写模型名（豆包需填火山方舟「推理接入点」ep-xxx）"},
            status_code=400,
        )

    provider = getattr(Config, "LLM_ACTIVE_PROVIDER", "deepseek")
    provider_label = (PROVIDERS.get(provider) or {}).get("label", provider)
    try:
        from openai import OpenAI
        client = OpenAI(api_key=api_key, base_url=base_url, timeout=15.0)
        # 最小验证：发一次 1-token 的补全请求，确认 key + 配额都可用
        client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": "ping"}],
            max_tokens=1,
        )
        return JSONResponse({"ok": True, "msg": f"✅ API Key 有效，{provider_label} 连接正常"})
    except Exception as e:
        return JSONResponse(
            {"ok": False, "msg": f"❌ 连接失败：{type(e).__name__}: {str(e)[:200]}"},
            status_code=400,
        )


@app.post("/api/settings/test_webhook")
async def api_test_webhook(request: Request):
    """
    向指定钉钉 webhook 发送一条测试消息（验证群机器人连通性）。
    """
    if not _is_admin(request):
        return _admin_forbidden()

    from notifier import _push_one, _build_markdown_message

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    url = (data.get("url") or "").strip()
    if not url:
        return JSONResponse({"ok": False, "msg": "❌ 未提供 webhook URL"}, status_code=400)
    if not url.startswith("https://oapi.dingtalk.com/"):
        return JSONResponse({"ok": False, "msg": "❌ 不是有效的钉钉 webhook 地址"}, status_code=400)

    test_items = [{
        "ai_title": "✅ 测试消息",
        "ai_summary": "这是一条来自 AI 行业动态聚合的设置测试消息，收到即代表 webhook 配置正确。",
        "category": "",
        "company_name": "",
        "source_name": "设置中心",
        "source_url": "",
    }]
    body = _build_markdown_message(test_items)
    ok = _push_one(url, body)
    return JSONResponse({
        "ok": ok,
        "msg": "✅ 测试消息已发送成功" if ok else "❌ 发送失败，请检查 webhook URL 是否正确",
    })


# ---------- 第七步：认证路由（登录/登出/找回/改密） ----------


def _safe_next(value: str) -> str:
    """校验登录后的跳转地址：只允许站内路径（以 / 开头、不以 // 开头），防开放重定向。"""
    value = (value or "").strip()
    if value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _logged_in(request: Request) -> bool:
    return auth.verify_session_token(request.cookies.get(auth.SESSION_COOKIE, "")) is not None


def _is_admin(request: Request) -> bool:
    """当前请求是否为管理员（会话里带出的角色）。用户身份访问管理接口一律拒绝。"""
    return getattr(request.state, "role", None) == "admin"


def _admin_forbidden() -> JSONResponse:
    """管理端权限不足的统一 403 响应。"""
    return JSONResponse({"ok": False, "msg": "❌ 无权限：仅管理员可操作"}, status_code=403)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    """登录页（含密码找回）。已登录则直接跳走。"""
    if _logged_in(request):
        return RedirectResponse(url=_safe_next(request.query_params.get("next", "/")), status_code=302)
    return templates.TemplateResponse(
        request=request,
        name="login.html",
        context={
            "app_title": Config.APP_TITLE,
            "next": _safe_next(request.query_params.get("next", "/")),
        },
    )


@app.post("/login")
async def login_submit(request: Request):
    """登录（JSON fetch 提交）：校验账号密码，成功后种会话 cookie。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)

    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    # 前端选的身份（管理员/用户）；登录成功后种该角色的会话 cookie
    role = (data.get("role") or "admin").strip()
    if not username or not password:
        return JSONResponse({"ok": False, "msg": "❌ 请输入用户名和密码"}, status_code=400)

    verified_role = auth.verify_login(username, password)
    # 用账号实际验证出的角色与声明的身份比对，防止用用户账号冒充管理员
    if not verified_role or verified_role != role:
        return JSONResponse({"ok": False, "msg": "❌ 用户名或密码错误"}, status_code=401)

    resp = JSONResponse({
        "ok": True,
        "msg": "登录成功",
        "redirect": _safe_next(data.get("next", "/")),
    })
    resp.set_cookie(
        key=auth.SESSION_COOKIE,
        value=auth.make_session_token(role),
        max_age=auth.AUTH_SESSION_DAYS * 86400,
        httponly=True,
        samesite="lax",
        path="/",
    )
    return resp


@app.post("/logout")
async def logout(request: Request):
    """登出：清除会话 cookie，跳回登录页。"""
    resp = RedirectResponse(url="/login", status_code=303)
    resp.delete_cookie(key=auth.SESSION_COOKIE, path="/")
    return resp


@app.post("/api/auth/request_reset")
async def api_auth_request_reset():
    """找回密码第一步：生成 6 位重置码并推送到所有钉钉群。"""
    ok, msg = auth.request_password_reset()
    return JSONResponse({"ok": ok, "msg": ("✅ " if ok else "❌ ") + msg}, status_code=200 if ok else 400)


@app.post("/api/auth/reset")
async def api_auth_reset(request: Request):
    """找回密码第二步：校验重置码 + 设置新密码。"""
    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    ok, msg = auth.reset_password(data.get("code", ""), data.get("new_password", ""))
    return JSONResponse({"ok": ok, "msg": ("✅ " if ok else "❌ ") + msg}, status_code=200 if ok else 400)


@app.post("/api/auth/change_password")
async def api_auth_change_password(request: Request):
    """修改管理员密码：验证旧密码 → 更新哈希（同时作废所有管理员旧会话，重新登录）。"""
    if not _is_admin(request):
        return _admin_forbidden()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    ok, msg = auth.change_password(data.get("old_password", ""), data.get("new_password", ""))
    resp = JSONResponse({"ok": ok, "msg": ("✅ " if ok else "❌ ") + msg}, status_code=200 if ok else 400)
    if ok:
        resp.delete_cookie(key=auth.SESSION_COOKIE, path="/")
    return resp


@app.post("/api/auth/set_user_password")
async def api_auth_set_user_password(request: Request):
    """修改用户（staff）密码（管理员操作）。成功后用户身份的所有旧会话作废。"""
    if not _is_admin(request):
        return _admin_forbidden()

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "msg": "❌ 请求体不是合法的 JSON"}, status_code=400)
    ok, msg = auth.set_user_password(data.get("new_password", ""))
    return JSONResponse({"ok": ok, "msg": ("✅ " if ok else "❌ ") + msg}, status_code=200 if ok else 400)


# ---------- 第七步半：日报页 ----------


@app.get("/reports", response_class=HTMLResponse)
async def reports_page(request: Request):
    """
    日报列表页（「日报」tab）。
    筛选：日期范围 / 领域 / 关键词；钉钉推送链接带 ?date=YYYY-MM-DD 精确到某天。
    """
    # ?date= 优先：钉钉"查看完整日报"按钮带它，定位到某一天
    date_param = _parse_date_param(request.query_params.get("date", ""))
    date_from = date_param or _parse_date_param(request.query_params.get("date_from", ""))
    date_to = date_param or _parse_date_param(request.query_params.get("date_to", ""))
    if date_from and date_to and date_from > date_to:
        date_from, date_to = date_to, date_from

    category = request.query_params.get("category", "")
    keyword = request.query_params.get("keyword", "").strip()
    page_str = request.query_params.get("page", "1")
    try:
        page = max(1, int(page_str))
    except ValueError:
        page = 1

    reports = query_reports(
        date_from=date_from, date_to=date_to, category=category,
        keyword=keyword, page=page, page_size=Config.PAGE_SIZE,
    )
    total = get_reports_total(date_from=date_from, date_to=date_to, category=category, keyword=keyword)
    total_pages = max(1, (total + Config.PAGE_SIZE - 1) // Config.PAGE_SIZE)

    # events / impact / strategy / trend 是 JSON 字符串，解析成列表供模板渲染。
    # 影响分析/战略意义/趋势观察是新版"要点数组"；旧日报在库里是纯文本段落，
    # 解析失败时 *_points 置 None → 模板回退到纯文本展示。
    for r in reports:
        try:
            r["events_list"] = json.loads(r.get("events") or "[]")
        except Exception:
            r["events_list"] = []
        for field in ("impact", "strategy", "trend"):
            pts = []
            try:
                val = json.loads(r.get(field) or "")
                if isinstance(val, list):
                    pts = [it for it in val if isinstance(it, dict) and (it.get("point") or it.get("detail"))]
            except Exception:
                pts = []
            r[f"{field}_points"] = pts or None

    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "app_title": Config.APP_TITLE,
            "active_tab": "reports",
            "reports": reports,
            "total": total,
            "total_pages": total_pages,
            "current_page": page,
            "current_category": category,
            "current_keyword": keyword,
            "current_date_from": date_from,
            "current_date_to": date_to,
            "has_reports": len(reports) > 0,
            "has_filters": bool(category or keyword or date_from or date_to),
            # API Key 是否已配置（与日报/问答共用同一 DeepSeek Key，状态统一显示）
            "has_api_key": bool(Config.DEEPSEEK_API_KEY),
            "scheduler_paused": is_scheduler_paused(),
            # 当前登录身份（双身份：管理员 / 用户）
            "current_username": getattr(request.state, "username", auth.AUTH_USERNAME),
            "current_role": getattr(request.state, "role", "admin"),
            # 今日 LLM 成本是否已达上限（页面 AI 状态栏显示「今日已暂停」）
            "llm_cost_blocked": cost_tracker.is_blocked(),
            # 当日是否已补全：所有有新闻的领域都已有日报 → 顶栏按钮渲染为禁用态；
            # 若仍有领域缺失（生成失败/未生成），按钮保持可点以便重试补齐
            "report_day_complete": (
                not report_missing_categories(shanghai_now().strftime("%Y-%m-%d"))
                and report_exists(shanghai_now().strftime("%Y-%m-%d"))
            ),
        },
    )


# ---------- RAG 问答 ----------

@app.get("/qa", response_class=HTMLResponse)
async def qa_page(request: Request):
    """问答页（「问答」tab）：单轮问答，答案 + 参考资料链接。"""
    return templates.TemplateResponse(
        request=request,
        name="qa.html",
        context={
            "app_title": Config.APP_TITLE,
            "active_tab": "qa",
            # API Key 是否已配置（问答与日报共用同一 DeepSeek Key，状态统一显示）
            "has_api_key": bool(Config.DEEPSEEK_API_KEY),
            "scheduler_paused": is_scheduler_paused(),
            # 当前登录身份（双身份：管理员 / 用户）
            "current_username": getattr(request.state, "username", auth.AUTH_USERNAME),
            "current_role": getattr(request.state, "role", "admin"),
            # 今日 LLM 成本是否已达上限（页面 AI 状态栏显示「今日已暂停」）
            "llm_cost_blocked": cost_tracker.is_blocked(),
        },
    )


@app.post("/api/qa")
async def api_qa(request: Request):
    """问答接口：检索 + LLM 生成答案，返回 {ok, answer, references, error?}。"""
    from qa_service import ask

    try:
        data = await request.json()
    except Exception:
        return JSONResponse({"ok": False, "error": "❌ 请求体不是合法的 JSON"}, status_code=400)

    question = (data.get("question") or "").strip()
    if not question:
        return JSONResponse({"ok": False, "error": "❌ 请输入问题"}, status_code=400)

    result = ask(question)
    if not result.get("ok"):
        return JSONResponse(result, status_code=400)
    return JSONResponse(result)


# ---------- 第八步：直接运行 ----------

if __name__ == "__main__":
    """
    直接运行 python main.py 启动开发服务器。

    学习注释：
      这样写的好处是：既可以用 python main.py 直接启动，
      也可以用 uvicorn main:app 启动（部署用）。
    """
    import uvicorn
    uvicorn.run(
        "main:app",
        host=Config.HOST,
        port=Config.PORT,
        # 开发时如需热重载，使用: uvicorn main:app --reload
        reload=False,
        log_level="info",
    )
