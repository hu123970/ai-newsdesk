# AI NewsDesk · AI 行业动态聚合器

> 自动采集国内外 AI 行业新闻 → 关键词预筛 → LLM 智能筛选与摘要 → 网页浏览 / 即时推送 / 每日日报 / 知识库问答

一个面向行业研究场景的 AI 资讯聚合平台：把散落在几十个 RSS 源、科技媒体和社交账号里的 AI 行业动态，收敛成一条干净的信息流，并自动产出结构化的每日行业日报。

---

## 功能特性

- **多源采集**：RSS 订阅聚合（含微博类源），URL 自动去重、发布时间归一化到东八区
- **关键词分级预筛**：先用零成本规则挡掉无关文章，只把可能相关的送进大模型
- **LLM 批量分析**：8 篇 / 请求打包分析，产出中文标题、摘要、领域分类、公司标签、地区
- **防幻觉校验链**：4 层确定性规则，拦截「编造公司 / 张冠李戴 / 标签无正文证据」三类典型错误
- **多供应商切换**：DeepSeek / 通义千问 / Kimi / 豆包 / 智谱 GLM 五选一，统一走 OpenAI 兼容接口
- **成本安全阀**：每日 LLM 花费超阈值自动暂停当日任务，次日恢复
- **钉钉推送**：相关新闻逐条推送到群，同一事件的多源报道自动合并成一张卡片
- **每日日报**：按领域汇总当日重点 + 跨时间趋势联想，定时生成并推送
- **RAG 问答**：对库内新闻 / 日报提问，答案按句标注 `[参考资料 N]` 并可点击溯源（无需向量库）
- **双身份登录**：管理员 / 普通用户，权限分级，密码支持钉钉重置码找回
- **全站热更新**：供应商、钉钉群、监控公司均可在设置弹窗修改，即时生效、无需重启

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.11+ / FastAPI / uvicorn |
| 前端 | Jinja2 + HTMX 2.0 + Tailwind CSS（几乎不写原生 JS） |
| 数据库 | SQLite（WAL 模式 + FTS5 全文索引） |
| 定时任务 | APScheduler |
| AI | openai SDK（OpenAI 兼容接口，可接多家供应商） |
| 推送 | 钉钉群机器人 Webhook |
| 采集中转（可选） | RSSHub + CookieCloud（微博等需要登录态的源） |

---

## 快速开始

### 环境要求

- Python 3.11+（Windows 上请确认 `py -3 -V` 能输出版本号，见下方「常见问题」）
- Node.js 22+（仅当需要 RSSHub 中转源时）

### 1. 启动聚合器（必需）

**最简单的方式：双击项目根目录下的 `start.bat`**，它会自动找到可用的 Python、检查依赖并启动。

手动启动（Windows PowerShell，推荐）：

```powershell
cd D:\dsh\ai-news-aggregator
py -3 -m pip install -r requirements.txt
py -3 main.py
```

CMD 用户把第一行换成 `cd /d D:\dsh\ai-news-aggregator`（`/d` 是 CMD 专有的跨盘符切换参数）。

macOS / Linux：

```bash
cd ai-news-aggregator
python3 -m pip install -r requirements.txt
python3 main.py
```

> ⚠️ Windows 上**不要**直接用 `python main.py`：系统自带的 `python.exe` 可能是
> Microsoft Store 的占位程序，运行后没有任何输出、页面也打不开。用 `py -3` 或 `start.bat`。

启动成功的标志（控制台最后几行）：

```
[启动] AI 行业动态聚合 v0.1.0
[数据库] 初始化完成：...\data\ai_news.db
[启动] 就绪！访问 http://localhost:9999 打开页面
INFO:     Uvicorn running on http://0.0.0.0:9999
```

浏览器打开 <http://127.0.0.1:9999> ，用下面的账号登录：

| 账号 | 角色 | 密码 | 权限 |
|------|------|------|------|
| `ai-news` | 管理员 | `12345678` | 全部功能（系统设置、手动采集、生成日报、改用户密码） |
| `staff` | 普通用户 | `12345678` | 浏览新闻 / 日报、使用问答 |

> 首次登录后请到「系统设置 → 修改登录密码」改掉默认密码。

### 2. 启动 RSSHub（可选，需要虎嗅 / 品玩 / 微博源时）

```powershell
cd D:\dsh\rsshub
pnpm start          # 默认监听 1200；仓库内已带依赖，可直接 start
# 没装 pnpm：
$env:NODE_ENV="production"; node dist/index.mjs
```

> RSSHub 首次启动需 30–60 秒加载路由，日志出现 `RSSHub is running on port 1200` 即就绪。

### 3. 启动 CookieCloud（可选，仅微博源需要，用于自动续期微博 Cookie）

```powershell
cd D:\dsh\cookiecloud-server\api
node app.js         # 默认监听 8088
```

三个服务的关系：

```
ai-news-aggregator (9999)  ──HTTP──▶  rsshub (1200)  ──▶  微博 / 虎嗅 / 品玩
                                    ▲
                                    └─ CookieCloud (8088) 定期同步浏览器里的微博 Cookie
```

> 只跑聚合器也能完整使用（海外媒体、国内主流媒体 RSS 均直连）；RSSHub 与 CookieCloud 只服务于「需要登录态或没有公开 RSS」的信息源。

---

## 目录结构

```
ai-news-aggregator/
├── main.py              # FastAPI 入口 + 路由 + 权限守卫
├── config.py            # .env → Config 静态配置
├── settings.py          # 运行时配置层（供应商/钉钉群/监控公司，热更新）
├── providers.py         # 5 家 LLM 供应商定义
├── cost_tracker.py      # 每日 LLM 成本记账 + 上限安全阀
├── auth.py              # 双身份登录、会话、找回/改密
├── database.py          # SQLite 读写 + FTS5 同步
├── models.py            # 数据类
├── collector.py         # RSS 采集 + 分级预筛 + 去重入库
├── llm_processor.py     # LLM 批量分析 + 结果校验链
├── daily_report.py      # 每日日报生成
├── qa_service.py        # RAG 问答
├── notifier.py          # 钉钉推送
├── scheduler.py         # 定时任务调度
├── prompts.py           # 提示词 + 监控公司 → 关键词/提示词重建
├── dedup.py             # 推送前同事件多源合并
├── weibo_cookie_reminder.py  # 微博 Cookie 到期钉钉提醒
├── timeutil.py          # 时区工具
├── sources.json         # 信息源配置
├── templates/           # Jinja2 模板
├── static/              # 样式与 Logo
├── data/                # SQLite 演示数据库
├── Dockerfile           # 容器镜像（Render / HF Spaces 通用）
├── render.yaml          # Render 一键部署
└── start.bat            # Windows 一键启动
```

---

## 核心流程

```
RSS 源 → collector 采集（httpx + feedparser）
  ├── 年龄过滤（只保留 N 天内）
  ├── 分级关键词预筛（公司词 / 强信号词 命中才放行）
  └── URL 去重入库 → items 表（待处理）

定时 LLM 处理（每 5 分钟一批）
  ├── 批量 8 篇/请求 → 解析 JSON 数组
  ├── 结果校验链（摘要交叉校验 / 无公司硬规则 / 白名单 / 正文证据）
  ├── 相关 → 写库 + 收集待推送
  └── 不相关 → 删除

推送 / 展示 / 日报 / 问答
  ├── 钉钉：同事件合并去重后群发
  ├── 网页：领域 / 地区 / 公司 / 关键词 / 日期筛选
  ├── 日报：按领域生成 + 推送
  └── 问答：问题 → 检索关键词 → FTS5/LIKE 检索 → LLM 生成带引用答案
```

---

## 设计要点（详见 [ARCHITECTURE.md](ARCHITECTURE.md)）

1. **关键词分级预筛**：公司词命中即放行；强信号词单独命中放行；弱泛词一律不放行，杜绝白烧 token。
2. **LLM 批量分析 + 降级**：整批输出被截断则降级逐条重试，单条解析失败再重试，仍失败才丢弃。
3. **防幻觉校验链**：宁可漏判、不可误判 —— 拿不准的直接判不相关，不让错误标签入库和推送。
4. **成本安全阀**：按供应商单价近似折算，超限即整日暂停，交互式问答不计入（防止一次提问打满预算）。
5. **推送去重**：用 LLM 强制生成的中文短标题做三级闸门相似度聚类，只影响推送，库与网页保留全部报道。
6. **RAG 无向量**：SQLite FTS5 trigram + 2 字中文词 LIKE 兜底，零新增依赖。
7. **三通道热更新**：写库 + 同步 Config + 重建 prompts 常量，改配置不用重启。

---

## 配置

`ai-news-aggregator/.env`（可复制 `.env.example`）主要项：

| 变量 | 说明 |
|------|------|
| `DEEPSEEK_API_KEY` | LLM API Key（也可在页面「系统设置」里填，存数据库） |
| `DEEPSEEK_MODEL` / `DEEPSEEK_BASE_URL` | 模型名 / OpenAI 兼容接口地址 |
| `SESSION_SECRET` | 会话签名密钥（改了全员登出） |
| `COLLECT_CRON_HOURS` | 定时采集时间点（如 `8,20`） |
| `PROCESS_INTERVAL_MINUTES` | LLM 处理间隔（默认 5 分钟） |
| `REPORT_CRON_HOUR` / `REPORT_CRON_MINUTE` | 日报生成时间（默认 20:30） |
| `MAX_ITEM_AGE_DAYS` | 只采集 N 天内的内容 |
| `PORT` | 服务端口 |
| `DINGTALK_WEBHOOK_URL` | 首个钉钉群 Webhook（后续可在设置弹窗管理多个群） |
| `PUBLIC_BASE_URL` | 钉钉日报「查看完整日报」跳转用的对外地址 |

> 监控公司名单、钉钉群列表存在数据库 `settings` 表里，通过设置弹窗维护，不写 `.env`。

**本仓库不含任何真实凭据**：LLM API Key 与钉钉 Webhook 都在页面「系统设置」里填（存数据库，不落代码）；微博 Cookie 填到 `rsshub/.env` 的 `WEIBO_COOKIES`。

---

## 部署到公网（免费）

仓库内置了容器化与平台配置，可直接部署：

| 文件 | 用途 |
|---|---|
| `Dockerfile` | 通用镜像：非 root 运行、按 `$PORT` 监听、`/login` 健康检查 |
| `.dockerignore` | 构建时排除 `.env` / `logs` / 文档 |
| `render.yaml` | Render 一键 Blueprint（免费套餐、自动生成 `SESSION_SECRET`） |

**Render 部署（推荐，约 5 分钟）**

1. 把仓库推到 GitHub；
2. Render → **New +** → **Web Service** → 选中本仓库；
3. Runtime 选 **Docker**、Instance 选 **Free**、Region 选 **Singapore**、Health Check Path 填 `/login`；
4. Environment 加三条：

   | Key | Value |
   |---|---|
   | `SESSION_SECRET` | `py -3 -c "import secrets;print(secrets.token_hex(32))"` 的输出 |
   | `APP_TITLE` | `AI 行业动态聚合` |
   | `COLLECT_CRON_HOURS` | `4`（只在凌晨采集，避免演示时打扰） |

5. 点 **Create Web Service**，拿到 `https://xxx.onrender.com`。

免费版限制：15 分钟无访问会休眠（首次打开等 30–60 秒）；磁盘是临时的，重启后数据库回到镜像里的初始版本。

> ⚠️ **Vercel 不适合本项目**：文件系统只读（SQLite 写不了）、没有常驻进程（APScheduler 不触发）、函数有时长上限。
> 备选平台：**Hugging Face Spaces**（Docker、免费、无需信用卡，`app_port` 填 `8000`）；面试当天也可用 `cloudflared tunnel --url http://127.0.0.1:9999` 临时给公网地址。

---

## 常见问题（启动排障）

| 现象 | 原因 | 解决 |
|------|------|------|
| `python main.py` **毫无反应**，也不报错，`127.0.0.1:9999` 打不开 | 系统里的 `python` 是 Microsoft Store 的占位程序（执行后静默失败） | 用 `py -3 main.py`，或直接双击 `start.bat` |
| 命令提示「不是内部或外部命令」 | 路径写成了相对路径但当前目录不对 | 用绝对路径：PowerShell 里 `cd D:\dsh\ai-news-aggregator`，CMD 里 `cd /d D:\dsh\ai-news-aggregator` |
| `ModuleNotFoundError: No module named 'fastapi'` | 依赖没装，或装到了另一个 Python 版本上 | `py -3 -m pip install -r requirements.txt`（务必用同一个解释器） |
| 窗口一闪就关，看不到报错 | 双击 `main.py` 时异常退出 | 双击 `start.bat`，它会在退出后 `pause` 停住并显示报错 |
| 启动报 `address already in use` | 9999 端口被占用（可能已开着一个） | 关掉旧窗口，或改 `.env` 里的 `PORT` |
| 页面能开但侧边栏显示「AI 待配置」 | 没有配 LLM API Key（属正常，不影响浏览） | 登录后进「系统设置 → AI 模型」填 Key |
| 采集报 `Cookies expired` / 微博源为空 | 微博 Cookie 已过期 | 重新登录微博，把 Cookie 整串贴到 `rsshub/.env` 的 `WEIBO_COOKIES` |

自检命令（确认解释器与依赖一次到位）：

```bat
py -3 -V
py -3 -c "import fastapi, uvicorn, jinja2, feedparser, httpx, bs4, openai, apscheduler, dotenv, Crypto; print('deps ok')"
```

---

## 说明

- 演示数据库内是 2026-07-28 ~ 2026-08-12 期间采集的公开新闻与据此生成的日报，用于展示完整效果。
- `data/` 与 `.env` 已在 `.gitignore` 中排除；如需把示例数据一起发布，请自行调整忽略规则。
