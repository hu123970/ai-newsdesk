# 架构说明 · AI NewsDesk

本文说明系统的模块划分、数据模型与关键设计取舍。

---

## 1. 分层与模块职责

```
┌─────────────────────────────────────────────────────────┐
│  Web 层   main.py（路由/权限守卫） · templates/ · static/ │
├─────────────────────────────────────────────────────────┤
│  业务层   collector · llm_processor · daily_report ·     │
│           qa_service · notifier · dedup                  │
├─────────────────────────────────────────────────────────┤
│  调度层   scheduler.py（APScheduler）                     │
├─────────────────────────────────────────────────────────┤
│  配置层   config.py（.env）· settings.py（运行时热更新）   │
│           prompts.py · providers.py · cost_tracker.py    │
├─────────────────────────────────────────────────────────┤
│  存储层   database.py（SQLite + FTS5）· models.py         │
│  认证层   auth.py（PBKDF2 + HMAC 会话）                   │
└─────────────────────────────────────────────────────────┘
```

| 模块 | 职责 | 关键点 |
|------|------|--------|
| `config.py` | 从 `.env` 读静态配置，构造 `Config` 类 | import 时冻结，故需要 `settings.py` 兜住"运行时可变" |
| `settings.py` | 运行时配置层：供应商、钉钉群、监控公司 | 写库 + 同步 `Config` + 重建 `prompts` 常量，三通道热更新 |
| `prompts.py` | 全部提示词 + 监控公司 → 关键词/提示词重建 + 分级预筛 | `quick_filter` 是零成本闸门 |
| `providers.py` | 5 家 LLM 供应商的默认 base_url / model | 全走 OpenAI 兼容接口，调用点无需感知供应商 |
| `auth.py` | 双身份登录、会话、找回/改密、HTTP 守卫 | 自写 HMAC 会话，免 JWT 依赖 |
| `database.py` | 建表、读写、FTS5 全文索引同步 | trigram 分词 + 2 字中文词 LIKE 兜底 |
| `collector.py` | RSS 采集、年龄过滤、预筛、URL 去重入库 | httpx + feedparser |
| `llm_processor.py` | 批量分析 → 解析 → 校验链 → 入库/删除 | 整批截断降级为逐条 |
| `daily_report.py` | 按领域生成日报 + 跨时间联想 + 推送 | 领域内当天至多一篇（唯一约束兜底） |
| `qa_service.py` | 检索 + 生成带引用的答案 | 无向量库，纯 FTS5 |
| `notifier.py` | 钉钉卡片构造与群发 | 必含关键词，单群失败不影响其他群 |
| `scheduler.py` | 定时采集 / 处理 / 日报 / Cookie 检查；暂停开关 | 暂停只影响定时任务，手动按钮不受影响 |
| `cost_tracker.py` | 每日 token × 单价折算，超限暂停 | 交互式问答不计入，防一次提问打满预算 |
| `dedup.py` | 推送前同事件多源合并 | 三级闸门相似度 |

---

## 2. 数据模型

```sql
items(id, source_url UNIQUE, source_name, title, content_text,
      ai_title, ai_summary, category, company_name, region,
      published_at, created_at, is_relevant)

reports(id, report_date, category, title, summary,
        events, impact, strategy, trend,     -- 均为 JSON
        source_ids, source_count, related_report_ids,
        raw_markdown, created_at,
        UNIQUE(report_date, category))

settings(key PRIMARY KEY, value)             -- 单表 KV，所有运行时配置

items_fts(body)      -- FTS5 trigram，与 items 同步（触发器/手动同步）
reports_fts(body)    -- FTS5 trigram
```

`settings` 表的键：

| 键 | 含义 |
|----|------|
| `llm_providers` | 供应商配置 JSON `{pid: {api_key, base_url, model}}` |
| `llm_active_provider` | 当前使用的供应商 id |
| `dingtalk_webhooks` | 群列表 JSON `[{name, url}]` |
| `target_companies` | 监控公司 JSON `[{name, region, category, keywords}]` |
| `auth_password_hash` / `user_password_hash` | 两个身份各自的 PBKDF2 哈希 |
| `session_secret` | 会话签名密钥（`.env` 未配时使用） |
| `reset_code` | 找回密码的重置码状态（哈希 + 过期时间 + 尝试次数） |
| `llm_cost_daily` | 当日成本累计 `{date, total_yuan}` |
| `deepseek_api_key` | 老版本单 Key 字段（仅作迁移来源，保留兼容） |

---

## 3. 关键设计

### 3.1 认证与会话

- 密码：`hashlib.pbkdf2_hmac('sha256', pw, salt, 200000)`，存储格式 `pbkdf2$iter$salt_hex$hash_hex`，免 bcrypt 依赖。
- 会话：`base64("expiry:role:prefix").hmac_sha256`，7 天有效。`prefix` 是该身份密码哈希的 SHA-256 前 8 位 —— **改密码即让该身份所有旧会话自动失效**，无需维护黑名单。
- 双身份各自独立哈希键，管理员改密码不会把普通用户踢下线。
- 中间件按请求类型分派未登录响应：HTMX → `401 + HX-Redirect`；`/api/*` → `401 JSON`；页面 → `302 /login?next=...`。

### 3.2 三条热更新通道

`Config` 在 import 时就从 `.env` 冻结了，但所有读取点都在"调用时"。因此保存设置时同时做三件事：

1. 写 `settings` 表（持久化）；
2. 覆盖 `Config` 的对应类属性（LLM key / webhook 列表）；
3. 调 `prompts.reload_prompts()` 重建监控公司相关的提示词常量与关键词表。

下一次调用自然吃到新值 —— 改配置不用重启进程。

### 3.3 采集与预筛（省 token 的第一道闸门）

只有命中「目标公司名 / 别名」或「AI 强信号词（大模型、具身智能、算力…）」的文章才入库待分析；命中「弱泛词（agent、robot、safety）」不放行 —— 这些词在泛科技媒体篇篇出现，送进模型也必被拒。

### 3.4 LLM 批量分析与校验链

- 8 篇 / 请求打包，输出必须是带编号的 JSON 数组，摊薄系统提示词的输入成本。
- `finish_reason == "length"` 视为整批失败 → 降级逐条分析；单条解析失败 → 单独重试，再失败才删除。
- 入库前过 4 层确定性校验（模型无关）：

| 关卡 | 拦截什么 | 处理 |
|------|---------|------|
| 摘要交叉校验 | 判相关但摘要自带"未提及目标公司" | 改判不相关 |
| 无公司硬规则 | 判相关却没给任何目标公司 | 改判不相关 |
| 白名单硬规则 | 输出了监控名单外的公司 | 改判不相关；命中别名回写规范名 |
| 正文证据校验 | 贴了标签但正文里没有这家公司 | 有据保留 / 唯一有据则回写 / 都无据改判 |

> 取舍：宁可漏判，不可误判。

### 3.5 推送去重

入库时不同媒体的同一事件可能多篇并存。推送前按 `ai_title` 相似度聚类，三级闸门：`≥0.70` 直接判同事件；`≥0.40` 且共享强令牌（英文词/数字）；`≥0.55` 且共享中文令牌（去泛词的中文二元组）。每簇合并为一张卡片并标注"另有 N 个来源"。

只影响推送，数据库与网页保留全部报道。

### 3.6 RAG 问答（无向量库）

- `≥3` 字词走 `items_fts MATCH`（trigram 分词）；
- `2` 字中文词 trigram 匹配不到，退回 `LIKE` 兜底；
- 新闻与日报统一编号后拼进上下文，答案按句标注 `[参考资料 N]`，前端可点击跳转。

### 3.7 成本安全阀

按供应商单价近似常量折算每日花费，累计超阈值即把当日的新闻判定与日报任务整体暂停，次日自动恢复；问答等交互能力不参与计算。

---

## 4. 扩展点

| 想做什么 | 改哪里 |
|---|---|
| 加信息源 | `sources.json` 追加 `{name, source_type:"rss", url, region, category}` |
| 加监控公司 | 设置弹窗 → 监控公司（即时重建预筛关键词与提示词） |
| 加 LLM 供应商 | `providers.py` 的 `PROVIDERS` 加一项（OpenAI 兼容即可） |
| 改推送渠道 | `notifier.py` 内聚了卡片构造与发送，替换为企微/飞书只需改这一个文件 |
| 改日报结构 | `prompts.REPORT_SYSTEM_PROMPT` + `REPORT_OUTPUT_FORMAT`，`daily_report.py` 负责落库 |

---

## 5. 本地运行的三进程关系

```
ai-news-aggregator :9999  ──HTTP──▶  rsshub :1200  ──▶ 目标站点（微博/虎嗅/品玩）
        ▲                                  ▲
        │                                  │ 每 5 分钟拉取最新 Cookie 并热更新
        └──── 读取 ../rsshub/.env 做 Cookie 到期检查 ────┘
                                           │
                              cookiecloud :8088（存放浏览器同步来的加密 Cookie）
```

聚合器只通过 HTTP 依赖 RSSHub，通过 `../rsshub/.env` 读取 CookieCloud 配置做到期提醒（可用 `RSSHUB_ENV` 环境变量指定其他路径）。因此三个目录同级放置即可开箱运行。
