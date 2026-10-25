"""
=========================================================
每日 LLM 成本记账（cost_tracker.py）
=========================================================
职责：统计每日 LLM API 调用的 token 用量折算金额，
     一旦超过固定上限（1 元）→ 今日自动暂停所有 LLM 任务，
     次日（上海时区）自动重置恢复。
=========================================================
设计：
  - 单价表：各供应商 ¥/百万 token（近似值，写在代码常量里，
    只作安全阀不求精确；供应商调价时改这里即可）
  - 存储：settings 表键 llm_cost_daily = {"date":"YYYY-MM-DD","total_yuan":0.0}
    重启不丢、跨天自动重置（次日自动恢复）
  - 并发：threading.Lock() 保护读写（调度线程 + 请求线程并发记账）
  - 拦截：只在各 LLM 任务【入口】查 is_blocked()，超限即整任务跳过，
    不在重试循环里打断（避免"处理失败就删条目"的副作用）
  - 供应商名从 Config.LLM_ACTIVE_PROVIDER 读，与 providers.PROVIDERS
    的供应商 id 一一对应（默认 deepseek）
=========================================================
"""

import json
import logging
import threading

from config import Config
from database import get_settings, set_setting
from timeutil import shanghai_now

logger = logging.getLogger("cost_tracker")

# 每日 LLM 成本上限（元）——固定 1 元，不做设置项。
# 触发即判定为异常：今日自动暂停所有 LLM 任务，次日自动恢复。
DAILY_LLM_LIMIT_YUAN = 1.0

# 供应商单价表（¥ / 百万 token，近似值，只作安全阀不求精确）。
# key 与 providers.PROVIDERS 的供应商 id 一致。
PROVIDER_PRICES_PER_M = {
    "deepseek": {"input": 2.0,  "output": 3.0},
    "qwen":     {"input": 1.0,  "output": 2.0},
    "kimi":     {"input": 4.0,  "output": 16.0},
    "doubao":   {"input": 1.0,  "output": 3.0},
    "glm":      {"input": 1.0,  "output": 2.0},
}
# 未知供应商兜底单价（按最高档算，宁多记不少记，安全侧）
_FALLBACK_PRICE = {"input": 4.0, "output": 16.0}

# settings 表存储键
_SETTING_KEY = "llm_cost_daily"

# 读写锁：调度线程（LLM 处理/日报）与请求线程（问答）并发记账
_lock = threading.Lock()


# ---------- 内部读写 ----------

def _load() -> dict:
    """读取当前记账；格式异常时按全新的一天处理。"""
    raw = (get_settings() or {}).get(_SETTING_KEY, "") or ""
    if raw:
        try:
            data = json.loads(raw)
            if (
                isinstance(data, dict)
                and data.get("date")
                and isinstance(data.get("total_yuan"), (int, float))
            ):
                return data
        except (json.JSONDecodeError, TypeError):
            pass
    return {"date": "", "total_yuan": 0.0}


def _save(data: dict):
    set_setting(_SETTING_KEY, json.dumps(data, ensure_ascii=False))


def _ensure_today() -> dict:
    """确保记账对应当天（上海时区）；跨天自动重置为 0。"""
    data = _load()
    today = shanghai_now().strftime("%Y-%m-%d")
    if data["date"] != today:
        logger.info(
            f"成本记账跨天重置：{data['date'] or '无记录'} → {today}（上限 ¥{DAILY_LLM_LIMIT_YUAN}）"
        )
        data = {"date": today, "total_yuan": 0.0}
        _save(data)
    return data


# ---------- 对外 API ----------

def is_blocked() -> bool:
    """今日 LLM 是否已达成本上限（True=暂停，False=正常）。跨天自动重置。"""
    with _lock:
        data = _ensure_today()
        blocked = data["total_yuan"] >= DAILY_LLM_LIMIT_YUAN
        if blocked:
            logger.warning(
                f"今日 LLM 成本已达上限 ¥{data['total_yuan']:.4f} ≥ ¥{DAILY_LLM_LIMIT_YUAN}，"
                f"已自动暂停（次日恢复）"
            )
        return blocked


def get_today_cost() -> float:
    """今日已用金额（元），设置弹窗「今日用量」只读显示。"""
    with _lock:
        return _ensure_today()["total_yuan"]


def record_usage(usage) -> None:
    """LLM 成功响应后记账一次：token 用量 × 当前供应商单价累加。"""
    if usage is None:
        return
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    prompt_tokens = max(prompt_tokens, 0)
    completion_tokens = max(completion_tokens, 0)

    provider = getattr(Config, "LLM_ACTIVE_PROVIDER", "deepseek") or "deepseek"
    price = PROVIDER_PRICES_PER_M.get(provider) or _FALLBACK_PRICE
    cost_yuan = (
        prompt_tokens * price["input"] + completion_tokens * price["output"]
    ) / 1_000_000

    with _lock:
        data = _ensure_today()
        data["total_yuan"] = round(data["total_yuan"] + cost_yuan, 8)
        _save(data)
        logger.info(
            f"成本记账：{provider} 本次 ¥{cost_yuan:.6f}"
            f"（in {prompt_tokens} / out {completion_tokens} tokens），"
            f"今日累计 ¥{data['total_yuan']:.4f} / ¥{DAILY_LLM_LIMIT_YUAN}"
        )


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python cost_tracker.py 验证核心逻辑（用临时数据库，不污染真实 data/ai_news.db）。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    import os
    import tempfile
    from datetime import timedelta
    from database import init_database

    temp_db = tempfile.mktemp(suffix=".db")
    Config.DATABASE_PATH = temp_db
    init_database()

    class _FakeUsage:
        def __init__(self, p, c):
            self.prompt_tokens = p
            self.completion_tokens = c

    try:
        # 1. 全新一天：未超限
        assert is_blocked() is False, "新的一天不应被阻断"
        print("✅ 新的一天 is_blocked=False")

        # 2. 记账累加：deepseek 单价 2.0/3.0，500k in + 100k out = 1.0 + 0.3 = 1.3 元
        Config.LLM_ACTIVE_PROVIDER = "deepseek"
        record_usage(_FakeUsage(500_000, 100_000))
        cost = get_today_cost()
        assert abs(cost - 1.3) < 1e-6, f"记账金额错误：{cost}"
        print(f"✅ 记账累加正确：¥{cost}")

        # 3. 超限阻断：1.3 ≥ 1.0
        assert is_blocked() is True, "累计超限应被阻断"
        print("✅ 超限 is_blocked=True")

        # 4. 跨天重置：把记账日期改到昨天 → 自动重置为 0 且不再阻断（次日自动恢复）
        yesterday = (shanghai_now() - timedelta(days=1)).strftime("%Y-%m-%d")
        set_setting(_SETTING_KEY, json.dumps({"date": yesterday, "total_yuan": 9.0}))
        assert is_blocked() is False, "跨天应自动重置"
        assert abs(get_today_cost() - 0.0) < 1e-9, "跨天重置后今日金额应为 0"
        print("✅ 跨天自动重置正确（次日自动恢复）")

        # 5. 其它供应商单价：qwen 1.0/2.0，100k in + 100k out = 0.1 + 0.2 = 0.3 元
        Config.LLM_ACTIVE_PROVIDER = "qwen"
        record_usage(_FakeUsage(100_000, 100_000))
        cost = get_today_cost()
        assert abs(cost - 0.3) < 1e-6, f"qwen 记账金额错误：{cost}"
        print(f"✅ 供应商单价切换正确：qwen ¥{cost}")

        print("=" * 50)
        print("cost_tracker 全部断言通过 🎉")
    finally:
        try:
            os.unlink(temp_db)
        except OSError:
            pass
