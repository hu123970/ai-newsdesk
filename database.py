"""
=========================================================
数据库模块（database.py）
=========================================================
职责：封装所有 SQLite 数据库操作，其他模块不直接写 SQL。
=========================================================
学习注释：
  这个模块是"数据层"，负责跟数据库打交道。
  其他模块（如采集器、页面）要读写数据时，调用这里的函数就行，
  不需要知道数据库是怎么存的——你以后如果换成 PostgreSQL，
  只需要改这一个文件，其他模块不用动。
=========================================================
"""

import sqlite3
from datetime import datetime, timedelta
from typing import List, Optional

from config import Config
from models import DisplayItem, FilterOptions


# ---------- 数据库连接管理 ----------

def get_connection() -> sqlite3.Connection:
    """
    获取数据库连接。

    学习注释：
      sqlite3.connect() 如果文件不存在会自动创建。
      row_factory = sqlite3.Row 让查询结果可以像字典一样用列名访问，
      比如 row["title"] 而不是 row[0]。
    """
    conn = sqlite3.connect(Config.DATABASE_PATH)
    conn.row_factory = sqlite3.Row
    return conn


# ---------- 建表 ----------

def init_database():
    """
    初始化数据库 —— 创建所有需要的表。

    学习注释：
      这个函数在应用启动时调用。
      IF NOT EXISTS 表示如果表已存在就不重复创建，
      所以多次运行也不会出错。
    """
    conn = get_connection()
    cursor = conn.cursor()

    # WAL 模式：允许读写并发，缓解设置写入与调度器 LLM 写入的锁竞争
    # （该设置持久化到数据库文件，执行一次即可）
    cursor.execute("PRAGMA journal_mode=WAL")

    # ---- settings 表：运行时配置（key-value） ----
    # 存 DeepSeek API Key、钉钉 webhook 列表、监控公司列表等
    # 与 .env 的关系：首次启动用 .env 做种子，之后以数据库为准
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    """)

    # ---- items 表：存储所有采集到的动态 ----
    # source_url UNIQUE：同样的 URL 不会重复插入（URL 去重）
    # is_relevant：NULL=还没处理，0=不相关，1=相关
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            source_url TEXT UNIQUE NOT NULL,
            source_name TEXT NOT NULL,
            title TEXT NOT NULL,
            content_text TEXT,
            ai_title TEXT,
            ai_summary TEXT,
            category TEXT,
            company_name TEXT,
            region TEXT,
            published_at TIMESTAMP NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            is_relevant INTEGER DEFAULT NULL
        )
    """)

    # ---- 索引：加快查询速度 ----
    # 按发布时间倒序排列（最新的在最前）
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_published_at
        ON items(published_at DESC)
    """)
    # 按领域分类筛选
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_category
        ON items(category)
    """)
    # 按是否相关筛选
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_is_relevant
        ON items(is_relevant)
    """)
    # 按公司和发布时间联合查询
    cursor.execute("""
        CREATE INDEX IF NOT EXISTS idx_items_company_published
        ON items(company_name, published_at DESC)
    """)

    # ---- reports 表：每日日报 ----
    # report_date：上海时区的日期（YYYY-MM-DD），category 三领域之一
    # events：JSON 数组 [{"title":..,"impact":..}]；source_ids：涉及新闻 id 的 JSON
    # trend：跨时间联想（趋势观察），由 LLM 结合历史生成
    # UNIQUE(report_date, category)：同一天同一领域只生成一次
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS reports (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            report_date TEXT NOT NULL,
            category TEXT NOT NULL,
            title TEXT NOT NULL,
            summary TEXT,
            events TEXT,
            impact TEXT,
            strategy TEXT,
            trend TEXT,
            source_ids TEXT,
            source_count INTEGER DEFAULT 0,
            related_report_ids TEXT,
            raw_markdown TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(report_date, category)
        )
    """)
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_date ON reports(report_date DESC)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_reports_category ON reports(category)")

    # ---- FTS5 全文检索表（RAG 问答用，零新增依赖） ----
    # trigram 分词：中文任意子串都能建索引，但实测不匹配 2 字词，
    # 所以 RAG 检索时对 ≤2 字关键词要做基础表 LIKE 回退（qa_service 里处理）。
    # 独立表（不挂 content=），rowid 与 items.id / reports.id 一一对应。
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(body, tokenize='trigram')
    """)
    cursor.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS reports_fts USING fts5(body, tokenize='trigram')
    """)

    conn.commit()
    conn.close()
    print(f"[数据库] 初始化完成：{Config.DATABASE_PATH}")

    # FTS 自愈同步：日常写入已实时增量维护索引，
    # 这里只做廉价条数比对，不一致才全量重建（见 sync_fts_if_needed）
    sync_fts_if_needed()


# ---------- 数据写入 ----------

def insert_item(
    source_url: str,
    source_name: str,
    title: str,
    content_text: str,
    published_at: datetime,
    region: str = "",
) -> Optional[int]:
    """
    插入一条原始数据（采集后用）。

    参数：
      source_url: 原文链接（用于去重）
      source_name: 来源名称
      title: 原始标题
      content_text: 正文内容
      published_at: 发布时间
      region: 地区（overseas/china）

    返回：
      新插入的 ID，如果已存在则返回 None

    学习注释：
      INSERT OR IGNORE：如果 source_url 已存在（UNIQUE 约束），
      就忽略插入不报错。这就是"URL 去重"的实现。
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR IGNORE INTO items
            (source_url, source_name, title, content_text, published_at, region)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (source_url, source_name, title, content_text, published_at, region))
        conn.commit()
        # 如果影响了0行，说明已存在（去重）
        if cursor.rowcount == 0:
            return None
        item_id = cursor.lastrowid
        conn.close()
        _sync_item_fts(item_id)  # 同步全文索引（FTS）
        return item_id
    finally:
        conn.close()


def get_settings() -> dict:
    """
    读取全部运行时设置（settings 表）。

    返回：
      {key: value} 字典；表为空时返回空 dict
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT key, value FROM settings")
    rows = cursor.fetchall()
    conn.close()
    return {row["key"]: row["value"] for row in rows}


def set_setting(key: str, value: str):
    """
    写入一条运行时设置（不存在则插入，存在则覆盖）。

    学习注释：
      INSERT OR REPLACE：以 key 为主键，重复写入直接覆盖旧值。
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            INSERT OR REPLACE INTO settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
        """, (key, value))
        conn.commit()
    finally:
        conn.close()


def update_item_analysis(
    item_id: int,
    is_relevant: bool,
    ai_title: str,
    ai_summary: str,
    category: str,
    company_name: str,
    region: str = "",
):
    """
    更新 LLM 分析结果到条目上。

    参数：
      region: 公司地区（china/overseas），按公司归属判定，不是信源地区

    学习注释：
      采集时先插入原始数据，LLM 处理完后用这个函数更新。
      分开两步的好处：
      1. 采集和 LLM 处理解耦（可以分开调试）
      2. LLM 处理失败时，数据还在，可以重试
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("""
            UPDATE items
            SET is_relevant = ?,
                ai_title = ?,
                ai_summary = ?,
                category = ?,
                company_name = ?,
                region = ?
            WHERE id = ?
        """, (int(is_relevant), ai_title, ai_summary, category, company_name, region, item_id))
        conn.commit()
        conn.close()
        _sync_item_fts(item_id)  # LLM 分析写入新标题/摘要，同步刷新全文索引
    finally:
        conn.close()


# ---------- 数据读取 ----------


def _build_conditions(filters: FilterOptions):
    """
    构建 WHERE 条件与参数（query_items / get_total_count 共用）。

    返回：
      (conditions, params)；调用方用 " AND ".join(conditions) 拼 SQL。
      所有值一律 ? 绑定，禁止 f-string 拼接用户值（防 SQL 注入）。

    学习注释：
      关键词/日期/公司/分类/地区都在这一个函数里，保证分页统计
      （get_total_count）和列表查询（query_items）永远用同一套条件，
      不会出现"列表 20 条、总数却对不上"的失步问题。
    """
    conditions = ["is_relevant = 1"]   # 只展示相关条目（保持原语义）
    params = []

    # 按领域筛选
    if filters.category:
        conditions.append("category = ?")
        params.append(filters.category)

    # 按公司筛选（LIKE 匹配，兼容 "OpenAI、Google" 这种多公司字段）
    if filters.company_name:
        conditions.append("company_name LIKE ?")
        params.append(f"%{filters.company_name}%")

    # 按地区筛选
    if filters.region:
        conditions.append("region = ?")
        params.append(filters.region)

    # 关键词：跨标题/摘要/正文/公司名模糊匹配。
    # SQLite LIKE 对 ASCII 默认大小写不敏感（'v4' 能命中 'V4'），中文按精确字符匹配。
    if filters.keyword:
        kw = f"%{filters.keyword}%"
        conditions.append(
            "(title LIKE ? OR ai_title LIKE ? OR ai_summary LIKE ? "
            "OR content_text LIKE ? OR company_name LIKE ?)"
        )
        params.extend([kw] * 5)

    # 日期范围 [date_from, date_to] 闭区间。
    # published_at 存 ISO 字符串（如 '2026-08-04 15:39:23+08:00'），
    # 前 10 位恒为 YYYY-MM-DD，用裸日期前缀做字符串比较，
    # 与后面的时间/时区后缀无关，天然规避 +00:00 / +08:00 混存的问题。
    if filters.date_from:
        conditions.append("published_at >= ?")
        params.append(filters.date_from)  # 含当天 00:00 及之后全部
    if filters.date_to:
        # 次日 0 点前 = 当天最后一刻，保证当天所有记录都在区间内
        end = (datetime.strptime(filters.date_to, "%Y-%m-%d") + timedelta(days=1)).strftime("%Y-%m-%d")
        conditions.append("published_at < ?")
        params.append(end)

    return conditions, params


def query_items(filters: FilterOptions) -> List[DisplayItem]:
    """
    查询动态列表（供前端展示用）。

    参数：
      filters: 筛选条件（领域、地区、公司、页码等）

    返回：
      DisplayItem 列表，按发布时间倒序

    学习注释：
      这里的 SQL 查询用了参数化查询（?占位符），
      不要用 f-string 拼接 SQL，有 SQL 注入风险。
      只查 is_relevant=1 的条目，不相关的自动过滤掉。
    """
    conn = get_connection()
    cursor = conn.cursor()

    # ---------- 构建查询条件 ----------
    # Stage 2：未处理的条目（is_relevant IS NULL）也展示
    # Stage 3：LLM 处理后，is_relevant=1 的展示，is_relevant=0 的隐藏
    # 关键词/日期/公司/分类/地区统一由 _build_conditions 构建
    conditions, params = _build_conditions(filters)

    # ---------- 执行查询 ----------
    where_clause = " AND ".join(conditions)
    offset = (filters.page - 1) * filters.page_size

    cursor.execute(f"""
        SELECT id, title, ai_title, ai_summary, category, company_name, region,
               source_url, source_name, published_at
        FROM items
        WHERE {where_clause}
        ORDER BY is_relevant DESC, published_at DESC
        LIMIT ? OFFSET ?
    """, params + [filters.page_size, offset])

    rows = cursor.fetchall()
    conn.close()

    # ---------- 组装结果 ----------
    # 把数据库行转成 DisplayItem 对象
    items = []
    for row in rows:
        published_at = datetime.fromisoformat(row["published_at"])
        items.append(DisplayItem(
            id=row["id"],
            title=row["ai_title"] or row["title"],
            summary=row["ai_summary"] or "",
            category=row["category"] or "",
            company_name=row["company_name"] or "",
            region=row["region"] or "",
            url=row["source_url"],
            source_name=row["source_name"],
            published_at=published_at,
            published_at_str=format_relative_time(published_at),
        ))

    return items


def query_distinct_companies() -> List[str]:
    """
    获取所有出现过的公司名称（用于前端筛选下拉）。

    学习注释：
      数据库中 company_name 可能存储 "OpenAI、Google" 这样的多公司字符串，
      需要按顿号拆分后去重，让每个公司独立出现在下拉框中。
      同时清理可能的空白字符。
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT DISTINCT company_name
        FROM items
        WHERE is_relevant = 1 AND company_name != ''
        ORDER BY company_name
    """)
    rows = cursor.fetchall()
    conn.close()

    # 拆分顿号连接的多个公司名，去重排序
    companies = set()
    for row in rows:
        name = row["company_name"]
        if name:
            for part in name.split("、"):
                part = part.strip()
                if part:
                    companies.add(part)
    return sorted(companies)


def delete_item(item_id: int):
    """
    删除单条条目（LLM 判定不相关时直接清理，不保留）。

    学习注释：
      之前不相关的条目只是标记 is_relevant=0 然后隐藏，
      但用户要求直接删除——留着没用还占空间。
    """
    conn = get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("DELETE FROM items WHERE id = ?", (item_id,))
        cursor.execute("DELETE FROM items_fts WHERE rowid = ?", (item_id,))
        conn.commit()
    finally:
        conn.close()


def get_unprocessed_items(limit: int = 20) -> List[dict]:
    """
    获取未经过 LLM 处理的原始条目（供 LLM 处理器使用）。

    学习注释：
      LLM 处理器每轮只处理一批未处理的条目。
      is_relevant IS NULL 表示还没处理过。
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, title, content_text, source_name, source_url, published_at
        FROM items
        WHERE is_relevant IS NULL
        ORDER BY CASE WHEN region = 'china' THEN 0 ELSE 1 END,
                 published_at DESC
        LIMIT ?
    """, (limit,))
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def get_unprocessed_source_stats() -> list:
    """
    获取各信息源的未处理条目数量统计（供数据管理面板展示）。

    返回：
      [{"source_name": "Hugging Face Blog", "count": 833}, ...]
      按数量降序排列
    """
    conn = get_connection()
    cursor = conn.cursor()
    cursor.execute("""
        SELECT source_name, COUNT(*) as cnt
        FROM items
        WHERE is_relevant IS NULL
        GROUP BY source_name
        ORDER BY cnt DESC
    """)
    rows = cursor.fetchall()
    conn.close()
    return [{"source_name": row["source_name"], "count": row["cnt"]} for row in rows]


# ---------- 统计查询 ----------

def get_total_count(filters: FilterOptions) -> int:
    """获取符合条件的总条目数（分页用）。"""
    conn = get_connection()
    cursor = conn.cursor()

    conditions, params = _build_conditions(filters)
    where_clause = " AND ".join(conditions)
    cursor.execute(f"SELECT COUNT(*) FROM items WHERE {where_clause}", params)
    count = cursor.fetchone()[0]
    conn.close()
    return count


# ---------- 工具函数 ----------

def format_relative_time(dt: datetime) -> str:
    """
    将时间格式化为"刚刚"、"5分钟前"、"2小时前"等相对时间。

    学习注释：
      处理 aware datetime（带时区）和 naive datetime（不带时区）的兼容。
      RSS 来源的时间可能带时区信息（如 +0000），
      本地 datetime.now() 默认不带时区，需要统一处理。
    """
    from datetime import timezone

    # 兼容存量数据：早期 feedparser 路径（collector 方式二/三）会把 UTC 时间
    # 存成不带时区的 naive 值。这里把 naive 一律视为 UTC，再统一转 UTC 比较，
    # 保证"带时区"和"历史 naive"两种数据的相对时间都正确。
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    # 把 dt 也转成 UTC 统一比较
    dt = dt.astimezone(timezone.utc).replace(tzinfo=None)
    now = now.replace(tzinfo=None)

    diff = now - dt
    minutes = int(diff.total_seconds() / 60)
    if minutes < 1:
        return "刚刚"
    elif minutes < 60:
        return f"{minutes}分钟前"
    elif minutes < 1440:  # 24小时
        hours = minutes // 60
        return f"{hours}小时前"
    elif minutes < 10080:  # 7天
        days = minutes // 1440
        return f"{days}天前"
    else:
        # 超过7天显示具体日期
        return dt.strftime("%m月%d日")


# ---------- 全文索引（FTS5）同步 ----------

# items 表 → items_fts、reports 表 → reports_fts 各自索引哪些字段
# （两个表共用同一套"把行拼成 body"的逻辑，字段清单抽成常量避免重复）
_ITEM_FTS_COLS = ("title", "ai_title", "ai_summary", "content_text", "company_name", "source_name")
_REPORT_FTS_COLS = ("title", "summary", "events", "impact", "strategy", "trend")


def _fts_body(row, cols: tuple) -> str:
    """把一行的指定字段拼成 FTS 的 body（空值统一成空串）。"""
    return " ".join(str(row[k] or "") for k in cols)


def _sync_fts(table: str, rowid: int, select_sql: str, cols: tuple):
    """刷新单条全文索引（先删后插，body 覆盖指定字段）。"""
    conn = get_connection()
    try:
        conn.execute(f"DELETE FROM {table} WHERE rowid = ?", (rowid,))
        row = conn.execute(select_sql, (rowid,)).fetchone()
        if row:
            conn.execute(f"INSERT INTO {table}(rowid, body) VALUES (?, ?)", (rowid, _fts_body(row, cols)))
        conn.commit()
    finally:
        conn.close()


def _sync_item_fts(item_id: int):
    """刷新单条 items 的全文索引（先删后插，body 覆盖标题/摘要/正文/公司/来源）。"""
    _sync_fts(
        "items_fts", item_id,
        "SELECT title, ai_title, ai_summary, content_text, company_name, source_name FROM items WHERE id = ?",
        _ITEM_FTS_COLS,
    )


def _sync_report_fts(report_id: int):
    """刷新单条日报的全文索引。"""
    _sync_fts(
        "reports_fts", report_id,
        "SELECT title, summary, events, impact, strategy, trend FROM reports WHERE id = ?",
        _REPORT_FTS_COLS,
    )


def rebuild_fts():
    """全量重建 items_fts / reports_fts（启动时调用，几百行 <100ms）。"""
    conn = get_connection()
    try:
        conn.execute("DELETE FROM items_fts")
        conn.execute("DELETE FROM reports_fts")
        for r in conn.execute(
            "SELECT id, title, ai_title, ai_summary, content_text, company_name, source_name FROM items"
        ).fetchall():
            conn.execute("INSERT INTO items_fts(rowid, body) VALUES (?, ?)", (r["id"], _fts_body(r, _ITEM_FTS_COLS)))
        for r in conn.execute(
            "SELECT id, title, summary, events, impact, strategy, trend FROM reports"
        ).fetchall():
            conn.execute("INSERT INTO reports_fts(rowid, body) VALUES (?, ?)", (r["id"], _fts_body(r, _REPORT_FTS_COLS)))
        conn.commit()
    finally:
        conn.close()


def sync_fts_if_needed():
    """
    启动时的自愈式 FTS 一致性检查（替代无条件全量重建）。

    为什么需要：全量重建在库小时无感，但随数据量增长（几十万条）会
    拖慢启动。而日常 insert/update/delete 本就走增量同步维护索引，
    全量重建只是保险丝。这里把"保险丝"改成只在真正需要时才触发：
      条数一致 → 跳过（正常情况零成本）
      条数不一致 → 全量重建（自愈，无需人工干预）

    注意：条数比对能发现"漏插/漏删"，但对"同 id 内容变更未同步"
    无感知——不过那类变更都经由 _sync_item_fts / _sync_report_fts
    增量维护，不会走到这里。此检查定位为兜底而非主路径。
    """
    conn = get_connection()
    try:
        cnt_items   = conn.execute("SELECT count(*) FROM items").fetchone()[0]
        cnt_reports = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
        cnt_fts_i   = conn.execute("SELECT count(*) FROM items_fts").fetchone()[0]
        cnt_fts_r   = conn.execute("SELECT count(*) FROM reports_fts").fetchone()[0]
        if cnt_items != cnt_fts_i or cnt_reports != cnt_fts_r:
            print(
                f"[数据库] FTS 索引与业务表条数不一致"
                f"（items {cnt_fts_i}/{cnt_items}，reports {cnt_fts_r}/{cnt_reports}），全量重建…"
            )
            rebuild_fts()
        else:
            print(f"[数据库] FTS 索引一致（items {cnt_items} / reports {cnt_reports}），跳过重建")
    finally:
        conn.close()


# ---------- 日报 ----------


def _json_field(v):
    """list → JSON 字符串（入库存 JSON）；str 原样（兼容旧日报纯文本格式）。"""
    import json as _json
    return _json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v


def insert_report(
    report_date: str,
    category: str,
    title: str,
    summary: str,
    events: list,
    impact,     # list[{"point","detail"}] 或旧格式 str
    strategy,   # 同上
    trend,      # 同上
    source_ids: list,
    source_count: int,
    related_report_ids: list,
    raw_markdown: str,
) -> int:
    """
    写入一份日报（同一天同一领域已存在则更新，id 保持不变以保证 FTS 映射）。
    返回 report_id。

    学习注释：
      impact/strategy/trend 卡片式分点（2026-08-12）：新格式是要点数组，
      入库时 _json_field 序列化成 JSON 字符串；旧日报在库里是纯文本段落，
      str 原样写入，前端解析时兼容两种。
    """
    import json as _json
    conn = get_connection()
    try:
        conn.execute("""
            INSERT INTO reports
            (report_date, category, title, summary, events, impact, strategy, trend,
             source_ids, source_count, related_report_ids, raw_markdown)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(report_date, category) DO UPDATE SET
                title = excluded.title,
                summary = excluded.summary,
                events = excluded.events,
                impact = excluded.impact,
                strategy = excluded.strategy,
                trend = excluded.trend,
                source_ids = excluded.source_ids,
                source_count = excluded.source_count,
                related_report_ids = excluded.related_report_ids,
                raw_markdown = excluded.raw_markdown
        """, (
            report_date, category, title, summary,
            _json.dumps(events, ensure_ascii=False),
            _json_field(impact), _json_field(strategy), _json_field(trend),
            _json.dumps(source_ids), source_count, _json.dumps(related_report_ids),
            raw_markdown,
        ))
        conn.commit()
        row = conn.execute(
            "SELECT id FROM reports WHERE report_date = ? AND category = ?", (report_date, category)
        ).fetchone()
        report_id = row["id"]
        conn.close()
        _sync_report_fts(report_id)
        return report_id
    finally:
        conn.close()


def query_relevant_by_shanghai_date(target_date: str) -> List[dict]:
    """
    获取上海日期为 target_date 的全部相关条目（日报/当日素材用）。

    混存偏移的坑：published_at 里既有 +00:00 也有 +08:00（naive 按 UTC），
    字符串前 10 位是"存的时区"下的日期，直接按它筛会漏/错。
    所以先 SQL 粗筛放宽一天窗口，再用 timeutil 逐条精筛上海日期。
    """
    from timeutil import shanghai_date
    prev = (datetime.strptime(target_date, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, title, ai_title, ai_summary, category, company_name, region,
                   source_name, source_url, published_at
            FROM items
            WHERE is_relevant = 1
              AND substr(published_at, 1, 10) BETWEEN ? AND ?
            ORDER BY published_at DESC
        """, (prev, target_date)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if shanghai_date(d.get("published_at")) == target_date:
            out.append(d)
    return out


def query_historical_by_companies(companies: list, before_date: str, per_company: int = 5, cap: int = 25) -> List[dict]:
    """
    跨时间联想素材：取这些公司 before_date 之前的历史相关新闻。
    按公司分组各取前 per_company 条，总量封顶 cap。全走 SQL，零 LLM 成本。
    """
    if not companies:
        return []
    conn = get_connection()
    out, seen = [], set()
    try:
        for c in companies:
            like = f"%{c}%"
            rows = conn.execute("""
                SELECT id, title, ai_title, ai_summary, category, company_name, published_at
                FROM items
                WHERE is_relevant = 1 AND company_name LIKE ?
                  AND substr(published_at, 1, 10) < ?
                ORDER BY published_at DESC
                LIMIT ?
            """, (like, before_date, per_company)).fetchall()
            for r in rows:
                if r["id"] in seen:
                    continue
                seen.add(r["id"])
                out.append(dict(r))
            if len(out) >= cap:
                break
    finally:
        conn.close()
    return out[:cap]


def get_latest_reports(category: str, before_date: str, limit: int = 7) -> List[dict]:
    """取该领域 before_date 之前的最近 limit 篇日报（趋势记忆，含 summary + trend）。"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT id, report_date, category, title, summary, trend
            FROM reports
            WHERE category = ? AND report_date < ?
            ORDER BY report_date DESC
            LIMIT ?
        """, (category, before_date, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def query_reports(date_from: str = "", date_to: str = "", category: str = "",
                  keyword: str = "", page: int = 1, page_size: int = 20) -> List[dict]:
    """日报列表（前端「日报」tab）：日期范围 / 领域 / 关键词筛选，按日期倒序。"""
    conds, params = [], []
    if date_from:
        conds.append("report_date >= ?")
        params.append(date_from)
    if date_to:
        conds.append("report_date <= ?")
        params.append(date_to)
    if category:
        conds.append("category = ?")
        params.append(category)
    if keyword:
        kw = f"%{keyword}%"
        conds.append("(title LIKE ? OR summary LIKE ? OR impact LIKE ? OR strategy LIKE ? OR trend LIKE ? OR events LIKE ?)")
        params.extend([kw] * 6)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    conn = get_connection()
    try:
        rows = conn.execute(f"""
            SELECT * FROM reports {where}
            ORDER BY report_date DESC, category
            LIMIT ? OFFSET ?
        """, params + [page_size, (page - 1) * page_size]).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def report_exists(report_date: str, category: str = None) -> bool:
    """
    该日期是否已生成过日报。

    参数：
      category: 指定领域（如 "ai_model"）→ 查该领域当天是否已生成；
                不传（None）→ 查当天是否已生成过任意领域。

    用途：
      - 按领域锁：report_exists(date, cat)，配合 UNIQUE(report_date, category)
        保证每领域当天至多一篇（避免重复生成/重复推送）。
      - 当日是否有日报：report_exists(date)，判断当天是否已生成过任意领域。
    """
    conn = get_connection()
    try:
        if category:
            sql, params = (
                "SELECT 1 FROM reports WHERE report_date = ? AND category = ?",
                (report_date, category),
            )
        else:
            sql, params = "SELECT 1 FROM reports WHERE report_date = ? LIMIT 1", (report_date,)
        return conn.execute(sql, params).fetchone() is not None
    finally:
        conn.close()


def get_reports_total(date_from: str = "", date_to: str = "", category: str = "",
                      keyword: str = "") -> int:
    """日报列表总数（分页用），与 query_reports 共用同一套筛选。"""
    conds, params = [], []
    if date_from:
        conds.append("report_date >= ?")
        params.append(date_from)
    if date_to:
        conds.append("report_date <= ?")
        params.append(date_to)
    if category:
        conds.append("category = ?")
        params.append(category)
    if keyword:
        kw = f"%{keyword}%"
        conds.append("(title LIKE ? OR summary LIKE ? OR impact LIKE ? OR strategy LIKE ? OR trend LIKE ? OR events LIKE ?)")
        params.extend([kw] * 6)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    conn = get_connection()
    try:
        row = conn.execute(f"SELECT COUNT(*) FROM reports {where}", params).fetchone()
        return row[0]
    finally:
        conn.close()


# ---------- RAG 检索 ----------


def search_items_fts(query: str, limit: int = 30) -> List[dict]:
    """
    FTS 检索新闻（≥3 字关键词）。rank 越小越相关（bm25 为负数）。
    返回条目字典（含 source_url / published_at / source_name 供引用展示）。
    """
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT items.id, items.title, items.ai_title, items.ai_summary,
                   items.category, items.company_name, items.region,
                   items.source_name, items.source_url, items.published_at,
                   bm25(items_fts) AS rank
            FROM items_fts
            JOIN items ON items.id = items_fts.rowid
            WHERE items_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def search_items_like(keywords: list, limit: int = 30) -> List[dict]:
    """
    LIKE 兜底检索（trigram 不匹配 2 字词，短词必须走这条）。
    每个关键词各跑一次 LIKE，命中次数越多越靠前；返回带 hits 字段。
    """
    hits = {}
    conn = get_connection()
    try:
        for kw in keywords:
            like = f"%{kw}%"
            rows = conn.execute("""
                SELECT id, title, ai_title, ai_summary, category, company_name, region,
                       source_name, source_url, published_at
                FROM items
                WHERE is_relevant = 1 AND (
                    title LIKE ? OR ai_title LIKE ? OR ai_summary LIKE ?
                    OR content_text LIKE ? OR company_name LIKE ?)
            """, (like, like, like, like, like)).fetchall()
            for r in rows:
                rid = r["id"]
                if rid not in hits:
                    hits[rid] = {"row": dict(r), "hits": 0}
                hits[rid]["hits"] += 1
    finally:
        conn.close()
    items = [dict(h["row"], hits=h["hits"]) for h in hits.values()]
    items.sort(key=lambda x: (-x["hits"], str(x.get("published_at") or "")), )
    return items[:limit]


def search_reports_fts(query: str, limit: int = 10) -> List[dict]:
    """FTS 检索日报。rank 越小越相关。"""
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT reports.id, reports.report_date, reports.category, reports.title,
                   reports.summary, reports.events, reports.impact, reports.strategy,
                   reports.trend, reports.source_count,
                   bm25(reports_fts) AS rank
            FROM reports_fts
            JOIN reports ON reports.id = reports_fts.rowid
            WHERE reports_fts MATCH ?
            ORDER BY rank
            LIMIT ?
        """, (query, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_news_total() -> int:
    """获取新闻总数，以供前端显示"""
    conn = get_connection()
    try:
        total = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        return total
    finally:
        conn.close()

def get_news_today(today: str) -> int:
    """
    获取上海日期为 today(今日) 采集的新闻数量。
    先 SQL 粗筛放宽一天窗口，再用 timeutil 逐条精筛上海日期。
    """
    from timeutil import shanghai_date
    yesterday = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")
    conn = get_connection()
    try:
        rows = conn.execute("""
            SELECT published_at
            FROM items
            WHERE substr(published_at, 1, 10) BETWEEN ? AND ?
            ORDER BY published_at DESC
        """, (yesterday, today)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        if shanghai_date(d.get("published_at")) == today:
            out.append(d)
    return len(out)

def get_ten_sources() -> List[dict]:
    """
    获取新闻来源的前十名及其数量
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT source_name as name, COUNT(*) as count
            FROM items
            GROUP BY source_name
            ORDER BY count DESC
            LIMIT ?
            """, (10,)).fetchall()
        return [dict(r) for r in row]
    finally:
        conn.close()

def get_category_count() -> List[dict]:
    """
    获取 每个分类有多少条新闻，加上中文名称映射在前端
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT category as name, COUNT(*) as count
            FROM items
            GROUP BY category
            ORDER BY count DESC
            """).fetchall()
        return [dict(r) for r in row]
    finally:
        conn.close()

def get_region_count() -> List[dict]:
    """
    获取 每个地区有多少条新闻，加上中文名称映射在前端
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT region as name, COUNT(*) as count
            FROM items
            GROUP BY region
            ORDER BY count DESC
            """).fetchall()
        out = []
        for r in row:
             name = (r["name"] or "unknown").strip() or "unknown"   # 空字符串/NULL → unknown
             out.append({"name": name, "count": r["count"]})
        return out
    finally:
        conn.close()

def get_daily_count() -> List[dict]:
    """
    获取每天有几条新闻，先得到所有新闻的发布时间，再转为上海时区分组统计
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT published_at as time
            FROM items
            ORDER BY published_at DESC
            """).fetchall()
        from timeutil import shanghai_date
        from collections import Counter
        out = []
        for r in row:
            d = dict(r)
            date = shanghai_date(d["time"])
            out.append(date)
        return [{"date": date, "count": count} for date, count in sorted(Counter(out).items())]
    finally:
        conn.close()


def get_ten_company() -> List[dict]:
    """
    获取新闻公司来源的前十名及其数量
    """
    conn = get_connection()
    try:
        row = conn.execute("""
            SELECT company_name as name
            FROM items
            WHERE company_name != ''    
            """).fetchall()
        out = []
        for r in row:
            companies = r["name"].split("、")
            out.extend(companies)
        from collections import Counter
        return [{"name": name, "count": count} for name, count in Counter(out).most_common(10)]
    finally:
        conn.close()

        
# ---------- 测试入口 ----------

if __name__ == "__main__":
    """直接运行 python database.py 测试数据库初始化"""
    print("测试数据库初始化...")
    init_database()
    print("数据库初始化成功！")
    print(f"数据库文件位置：{Config.DATABASE_PATH}")
