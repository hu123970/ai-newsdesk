"""
=========================================================
配置模块（config.py）
=========================================================
职责：统一管理所有配置项，从 .env 文件和环境变量中读取。
=========================================================
学习注释：
  - 使用 python-dotenv 库从 .env 文件加载配置
  - 所有配置集中在这里，其他模块通过导入这个类来获取配置
  - 这样做的好处是：如果要改配置（比如换 LLM 供应商），
    只需要改 .env 文件和这里，其他模块不用动
=========================================================
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# ---------- 第一步：加载 .env 文件 ----------
# 找到项目根目录（也就是 .env 文件所在目录）
# __file__ 是当前文件（config.py）的路径
# .parent 取上一级目录，即项目根目录
PROJECT_ROOT = Path(__file__).parent

# 加载 .env 文件中的配置到环境变量
# override=True 表示如果环境变量已存在，用 .env 文件的覆盖
load_dotenv(PROJECT_ROOT / ".env", override=True)


# ---------- 第二步：定义配置类 ----------
class Config:
    """
    配置类，所有配置项都定义在这里。

    用法：
        from config import Config
        print(Config.DATABASE_PATH)  # 获取数据库路径

    学习注释：
        用类变量的方式定义配置，简单直观。
        如果要新增配置项，在这里加一行即可。
    """

    # ==================== LLM 配置 ====================

    # DeepSeek API Key（没有就填空字符串，LLM 功能暂时不可用）
    # 从 .env 文件的 DEEPSEEK_API_KEY 读取
    DEEPSEEK_API_KEY: str = os.getenv("DEEPSEEK_API_KEY", "")

    # DeepSeek 模型名称
    DEEPSEEK_MODEL: str = os.getenv("DEEPSEEK_MODEL", "deepseek-chat")

    # DeepSeek API 地址（不用改）
    DEEPSEEK_BASE_URL: str = os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com")

    # 当前使用的 LLM 供应商 id（deepseek/qwen/kimi/doubao/glm，见 providers.py）。
    # 运行时由 settings.load_runtime_settings() 从数据库同步，
    # 切换供应商后会把上面的 API_KEY/BASE_URL/MODEL 一起指向它。
    LLM_ACTIVE_PROVIDER: str = os.getenv("LLM_ACTIVE_PROVIDER", "deepseek")

    # ==================== 数据库配置 ====================

    # SQLite 数据库文件路径
    # 如果路径是相对路径，就拼上项目根目录
    _db_path: str = os.getenv("DATABASE_PATH", "data/ai_news.db")
    DATABASE_PATH: str = str(PROJECT_ROOT / _db_path)

    # ==================== 采集配置 ====================

    # 采集间隔（分钟）
    COLLECT_INTERVAL_MINUTES: int = int(os.getenv("COLLECT_INTERVAL_MINUTES", "60"))

    # 每天自动采集的时间点（24小时制，逗号分隔多个小时）
    # 默认：早上 8 点看昨晚新闻，晚上 8 点看白天新闻
    COLLECT_CRON_HOURS: str = os.getenv("COLLECT_CRON_HOURS", "8,20")

    # 只采集 N 天以内发布的内容（防止 RSS 源返回历史灌水）
    MAX_ITEM_AGE_DAYS: int = int(os.getenv("MAX_ITEM_AGE_DAYS", "1"))

    # ==================== 应用配置 ====================

    # 应用标题
    APP_TITLE: str = os.getenv("APP_TITLE", "AI 行业动态聚合")

    # 每页显示条数
    PAGE_SIZE: int = int(os.getenv("PAGE_SIZE", "50"))

    # 信息源配置文件路径
    SOURCES_PATH: str = str(PROJECT_ROOT / "sources.json")

    # ==================== LLM 批处理配置 ====================

    # 一次 API 请求打包分析的文章数（批量模式）
    # 受 DeepSeek max_tokens 硬上限 8192 限制，默认 8 篇/请求比较安全
    LLM_BATCH_SIZE: int = max(1, int(os.getenv("LLM_BATCH_SIZE", "8")))

    # LLM 处理间隔（分钟）：每轮消化一批待处理条目
    # 批量后单轮成本大幅下降，默认保持 5 分钟，拉长只会让钉钉推送变慢
    PROCESS_INTERVAL_MINUTES: int = int(os.getenv("PROCESS_INTERVAL_MINUTES", "5"))

    # ==================== 钉钉推送配置 ====================

    # 钉钉群机器人 Webhook URL（不配置则不推送）
    # 获取方式：钉钉群 → 群设置 → 智能群助手 → 添加机器人 → 自定义 → Webhook
    DINGTALK_WEBHOOK_URL: str = os.getenv("DINGTALK_WEBHOOK_URL", "")

    # 钉钉群机器人 Webhook 列表（群发用，运行时由 settings.py 维护）
    # 元素格式：[{"name": "核心群", "url": "https://oapi.dingtalk.com/..."}]
    # 首次启动会用上面的 DINGTALK_WEBHOOK_URL 种子化成一个群
    DINGTALK_WEBHOOKS: list = []

    # 推送请求超时（秒）
    NOTIFIER_TIMEOUT: int = int(os.getenv("NOTIFIER_TIMEOUT", "10"))

    # ==================== 推送去重配置 ====================

    # 推送前按 ai_title 相似度合并"同一事件的多源报道"。
    # 开启后：同一事件的 N 条报道合并成一张卡片，附"另有 N 个来源"。
    # 只影响钉钉推送，数据库 / 网页 / 日报仍保留全部报道。
    PUSH_DEDUP_ENABLED: bool = os.getenv("PUSH_DEDUP_ENABLED", "1") == "1"

    # 三级闸门阈值（详见 dedup.py 模块文档）：
    #   HIGH        —— 序列相似度 ≥ 此值直接判同事件
    #   LOW_STRONG  —— ≥ 此值且共享"强令牌"（英文词≥3字母/数字≥2位）判同事件
    #   LOW_CJK     —— ≥ 此值且共享"中文令牌"（去泛词的中文二元组）判同事件
    DEDUP_SIM_HIGH: float = float(os.getenv("DEDUP_SIM_HIGH", "0.70"))
    DEDUP_SIM_LOW_STRONG: float = float(os.getenv("DEDUP_SIM_LOW_STRONG", "0.40"))
    DEDUP_SIM_LOW_CJK: float = float(os.getenv("DEDUP_SIM_LOW_CJK", "0.55"))

    # ==================== 认证配置 ====================

    # 会话 cookie 签名密钥。建议写死一个随机值（改了全员登出）。
    # 留空则启动时自动生成并存入数据库（重启不失效）。
    SESSION_SECRET: str = os.getenv("SESSION_SECRET", "")

    # ==================== 日报配置 ====================

    # 每日日报生成时间（24 小时制，默认每天 20:30——20:00 采集后
    # LLM 每 5 分钟消化，20:30 已基本处理完，job 内还会先排空队列兜底）
    REPORT_CRON_HOUR: int = int(os.getenv("REPORT_CRON_HOUR", "20"))
    REPORT_CRON_MINUTE: int = int(os.getenv("REPORT_CRON_MINUTE", "30"))

    # 对外访问地址（钉钉日报推送里「查看完整日报」链接用它拼接）
    PUBLIC_BASE_URL: str = os.getenv("PUBLIC_BASE_URL", "http://127.0.0.1:9999").rstrip("/")

    # ==================== 运行配置 ====================

    # 开发服务器主机地址（0.0.0.0 表示监听所有网络接口）
    HOST: str = "0.0.0.0"

    # 开发服务器端口（从环境变量读取，默认 8000）
    PORT: int = int(os.getenv("PORT", "8000"))


# ---------- 第三步：快速测试（直接运行此文件时） ----------
if __name__ == "__main__":
    """直接运行 python config.py 可以查看当前配置值"""
    print("=" * 50)
    print("当前配置项：")
    print("=" * 50)
    for attr in dir(Config):
        # 只显示大写开头的配置项（不显示内置方法和属性）
        if attr.isupper():
            value = getattr(Config, attr)
            # 如果是 API Key，只显示前几位，保护隐私
            if "KEY" in attr and value:
                value = value[:8] + "..." + value[-4:]
            print(f"  {attr} = {value}")
    print("=" * 50)
