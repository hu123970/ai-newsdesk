"""
=========================================================
定时任务调度器模块（scheduler.py）— Stage 2 实现
=========================================================
职责：按设定间隔触发采集和 LLM 处理流程。
=========================================================
学习注释：
  使用 APScheduler（Advanced Python Scheduler）管理定时任务。
  相比 threading.Timer 或 asyncio，APScheduler 提供了：
  - 持久化任务（应用重启不丢失）
  - 多种触发器（间隔、cron 表达式、日期时间）
  - 线程/进程池管理
  - 任务跳过/合并策略（上一次还没跑完，下一次是否跳过）

  这里的调度逻辑：
  1. 应用启动后，每 N 分钟自动采集一次
  2. 采集完成后，调用 LLM 处理器（Stage 3 才生效）
  3. 提供手动触发接口，方便调试和紧急刷新
=========================================================
"""

import logging
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.schedulers.base import STATE_PAUSED
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

from config import Config

# ---------- 日志 ----------
logger = logging.getLogger("scheduler")

# ---------- 全局调度器实例 ----------
# 设置为 None，start_scheduler() 调用时才创建
_scheduler: Optional[BackgroundScheduler] = None

# ---------- 手动暂停标记 ----------
# True = 用户点了"暂停"，所有定时任务（采集 + LLM 处理）停止触发；
# False = 正常运行。重启进程后恢复为运行态（不持久化）。
_manual_paused = False


# ---------- 采集任务 ----------

def _collect_job():
    """定时采集任务：抓取最新内容并入库（含关键词预筛选）。"""
    from collector import collect_all_sources

    logger.info("⏰ 定时采集任务触发")
    try:
        new_count = collect_all_sources()
        logger.info(f"采集完成：新增 {new_count} 条")
    except Exception as e:
        logger.error(f"采集任务执行失败：{e}", exc_info=True)


# ---------- LLM 处理任务 ----------

def _process_job():
    """独立 LLM 处理任务：持续消化未处理队列。"""
    from llm_processor import process_unprocessed_items
    import cost_tracker

    if cost_tracker.is_blocked():
        logger.warning("今日 LLM 成本已达上限，跳过定时 LLM 处理（次日自动恢复）")
        return

    logger.info("🤖 定时 LLM 处理任务触发")
    try:
        processed = process_unprocessed_items(max_items=20)
        if processed > 0:
            logger.info(f"LLM 处理完成：处理了 {processed} 条")
        else:
            logger.info("LLM 处理：暂无待处理条目")
    except Exception as e:
        logger.error(f"LLM 处理任务执行失败：{e}", exc_info=True)


# ---------- 每日日报任务 ----------

def _daily_report_job():
    """每天生成并推送当日日报（按领域汇总，含跨时间联想）。"""
    import cost_tracker

    if cost_tracker.is_blocked():
        logger.warning("今日 LLM 成本已达上限，跳过定时日报生成（次日自动恢复）")
        return

    logger.info("📄 定时日报任务触发")
    try:
        from daily_report import generate_daily_report
        result = generate_daily_report()
        logger.info(f"日报生成完成：{result}")
    except Exception as e:
        logger.error(f"日报生成失败：{e}", exc_info=True)


# ---------- 微博 cookie 过期提醒任务 ----------

def _weibo_cookie_check_job():
    """每天检查微博 cookie（SUB）过期时间，剩≤30天时钉钉提醒所有群。"""
    logger.info("🔍 定时检查微博 cookie 过期时间")
    try:
        from weibo_cookie_reminder import check_and_notify
        result = check_and_notify()
        logger.info(f"微博 cookie 检查完成：{result}")
    except Exception as e:
        logger.error(f"微博 cookie 检查失败：{e}", exc_info=True)


# ---------- 调度器管理 ----------

def start_scheduler():
    """
    启动定时任务调度器。

    学习注释：
      BackgroundScheduler 在后台线程中运行，不会阻塞主程序。
      默认使用线程池执行任务，多个任务可以并发执行。

      coalesce=True 表示如果上一次任务还没执行完，
      下一次触发时间到了就跳过（合并），避免任务堆积。

      max_instances=1 表示同一个任务最多同时运行 1 个实例，
      防止采集任务并发执行导致重复数据。
    """
    global _scheduler

    if _scheduler is not None:
        logger.warning("调度器已经在运行中，跳过重复启动")
        return

    # 每天自动采集的时间点（24小时制，逗号分隔多个小时）
    # 例："8,20" = 每天早上 8 点和晚上 8 点各采集一次
    collect_hours = [int(h.strip()) for h in Config.COLLECT_CRON_HOURS.split(",") if h.strip().isdigit()]
    if not collect_hours:
        collect_hours = [8, 20]

    logger.info(f"启动定时调度器（自动采集 {collect_hours} 点各一次，LLM处理每 {Config.PROCESS_INTERVAL_MINUTES} 分钟）")

    _scheduler = BackgroundScheduler(
        daemon=True,  # 守护线程：主程序退出时自动关闭
    )

    # 任务一： 定时采集（每天指定小时的整点触发）
    # APScheduler 的 CronTrigger 不支持列表，多个小时要用逗号分隔字符串（如 "8,20"）
    hour_expr = ",".join(str(h) for h in collect_hours)
    _scheduler.add_job(
        _collect_job,
        trigger=CronTrigger(hour=hour_expr, minute=0),
        id="collect",
        name="采集",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # 任务二：LLM 处理（每 PROCESS_INTERVAL_MINUTES 分钟独立运行，消化未处理队列）
    _scheduler.add_job(
        _process_job,
        trigger=IntervalTrigger(minutes=Config.PROCESS_INTERVAL_MINUTES),
        id="llm_process",
        name="LLM处理",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # 任务三：微博 cookie 过期提醒（每天上午检查一次，剩≤30天推钉钉）
    # 选 09:03 而非整点，避免和定时采集（08:00/20:00）挤在一起
    _scheduler.add_job(
        _weibo_cookie_check_job,
        trigger=CronTrigger(hour=9, minute=3),
        id="weibo_cookie_check",
        name="微博cookie检查",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    # 任务四：每日日报（默认每天 20:30——20:00 采集后 LLM 每 5 分钟消化，
    # 20:30 已基本处理完；job 内还会先排空队列兜底尾部积压）
    _scheduler.add_job(
        _daily_report_job,
        trigger=CronTrigger(hour=Config.REPORT_CRON_HOUR, minute=Config.REPORT_CRON_MINUTE),
        id="daily_report",
        name="每日日报",
        replace_existing=True,
        coalesce=True,
        max_instances=1,
    )

    _scheduler.start()

    # 防御：若在调度器启动前就已处于"暂停"状态，启动后立即恢复暂停
    if _manual_paused:
        _scheduler.pause()

    logger.info("调度器启动成功！")

    # 启动后立即执行一次 LLM 处理（清积压），采集暂不自动运行
    import threading
    # threading.Thread(target=_collect_job, name="initial-collect", daemon=True).start()
    threading.Thread(target=_process_job, name="initial-process", daemon=True).start()


# ---------- 暂停 / 恢复控制 ----------

def is_scheduler_paused() -> bool:
    """当前是否处于手动暂停状态（供页面按钮初始状态使用）。"""
    return _manual_paused


def pause_scheduler():
    """暂停所有定时任务（定时采集 + LLM 处理），按钮从「暂停」变「开始」。"""
    global _manual_paused
    _manual_paused = True
    if _scheduler is not None:
        _scheduler.pause()
    logger.info("⏸ 定时采集已暂停（所有定时任务停止触发）")


def resume_scheduler():
    """恢复所有定时任务，按预设时间节点继续采集，按钮从「开始」变「暂停」。"""
    global _manual_paused
    _manual_paused = False
    # 仅当调度器处于暂停态才恢复，避免对运行态调用 resume() 抛错
    if _scheduler is not None and _scheduler.state == STATE_PAUSED:
        _scheduler.resume()
    logger.info("▶ 定时采集已恢复")


def toggle_scheduler() -> bool:
    """
    切换暂停 / 恢复状态。

    返回：
      切换后是否处于暂停状态（True=刚暂停，False=刚恢复）
    """
    if is_scheduler_paused():
        resume_scheduler()
        return False
    pause_scheduler()
    return True


def collect_now() -> int:
    """
    手动触发一次采集（不等待下次定时）。

    返回：
      本次采集的新增条目数

    学习注释：
      这个函数提供"手动采集"能力，通常在以下场景使用：
      - 用户在网页上点击"刷新"按钮
      - 调试时想立即看到采集效果
      - 不想等 30 分钟，想马上拿到最新内容
    """
    from collector import collect_all_sources
    logger.info("🔧 手动触发采集")
    return collect_all_sources()


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python scheduler.py 测试调度器。
    会启动调度器并立即执行一次采集。
    """
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )

    from database import init_database
    init_database()

    print("=" * 50)
    print("启动调度器测试...")
    print("=" * 50)

    start_scheduler()

    # 保持主线程运行，否则守护线程会立刻退出
    import time
    try:
        print("调度器运行中，按 Ctrl+C 停止...")
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n调度器测试结束。")
