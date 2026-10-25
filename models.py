"""
=========================================================
数据模型模块（models.py）
=========================================================
职责：定义所有数据结构和数据类，是项目中"名词"的集合。
=========================================================
学习注释：
  数据类（dataclass）是 Python 的一种轻量结构，适合定义"纯数据"。
  就像写表单时要先确定"每一栏填什么类型的数据"一样，
  这里先定义好所有数据结构，后面各个环节（采集、处理、展示）才会统一。
=========================================================
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional


# ==================== 采集阶段的数据结构 ====================

@dataclass
class RawItem:
    """
    原始条目 —— 采集器从信息源获取的原始内容。

    学习注释：
      这是"采集阶段"的输出，此时的内容还是原始的、未经处理的。
      is_rss 标记这条数据是从 RSS 还是网页抓取来的，
      方便后续排查问题。
    """
    # 原标题（从 RSS 或网页上直接拿到的）
    title: str
    # 原文链接（用于去重和跳转）
    url: str
    # 来源名称（如 "TechCrunch"、"36氪"）
    source_name: str
    # 发布时间
    published_at: datetime
    # 正文或摘要内容
    content_text: str
    # 是否来自 RSS（True=RSS，False=网页抓取）
    is_rss: bool = True


@dataclass
class SourceConfig:
    """
    信息源配置 —— 描述一个信息源的配置信息。

    学习注释：
      对应 sources.json 中每一条的格式。
      每个信息源需要这几个字段来描述：
      - name：显示名称
      - type：类型（rss 或 webpage）
      - url：地址
      - region：地区（overseas=海外，china=国内）
      - category：领域分类
    """
    # 来源显示名称
    name: str
    # 来源类型：rss（RSS订阅）| webpage（网页抓取）
    source_type: str
    # RSS 或网页地址
    url: str
    # 地区：overseas（海外）| china（国内）
    region: str
    # 领域分类：ai_model（AI大模型）| data_labeling（数据标注）| embodied_ai（具身智能）
    category: str = "ai_model"


# ==================== LLM 处理后的数据结构 ====================

@dataclass
class ProcessedItem:
    """
    已处理条目 —— 经过 LLM 过滤和摘要后的结果。

    学习注释：
      这是"LLM处理阶段"的输出。
      相比 RawItem，增加了 AI 生成的标题、摘要、分类等信息。
      is_relevant=False 表示这条内容不在监测范围内，应该丢弃。
    """
    # 原始数据（保留原始内容用于排查）
    raw: RawItem
    # LLM 判断：是否属于监测范围
    is_relevant: bool = False
    # AI 生成的一句话标题（10-20字）
    ai_title: str = ""
    # AI 生成的 2-3 句摘要（50-150字）
    ai_summary: str = ""
    # 领域分类：ai_model | data_labeling | embodied_ai
    category: str = ""
    # 涉及的公司名称
    company_name: str = ""


# ==================== 展示层的数据结构 ====================

@dataclass
class DisplayItem:
    """
    展示条目 —— 传给前端模板渲染用的数据。

    学习注释：
      这个类是最"干净"的数据版本，只包含前端需要展示的字段。
      不包含原始内容、不包含 LLM 处理中间状态。
      数据库查询出来的数据会转成这个格式再传给模板。
    """
    # 条目 ID（数据库主键）
    id: int
    # AI 生成标题（如果没有就用原标题）
    title: str
    # AI 生成摘要
    summary: str
    # 领域分类
    category: str
    # 涉及公司
    company_name: str
    # 原文链接
    url: str
    # 来源名称
    source_name: str
    # 发布时间（格式化成 "刚刚"/"30分钟前"/"2小时前" 等相对时间）
    published_at: datetime
    # 公司地区：china（国内公司）| overseas（海外公司），按公司归属而非信源
    region: str = ""
    # 发布时间（格式化成 "2024-07-30 14:30" 这种）
    published_at_str: str = ""


@dataclass
class FilterOptions:
    """
    筛选选项 —— 用户在前端选择的筛选条件。

    学习注释：
      用户点击"AI大模型"、"海外"等筛选按钮时，
      这些条件会被封装成这个对象传给后端查询。
    """
    # 领域分类筛选（空字符串=全部）
    category: str = ""
    # 地区筛选
    region: str = ""
    # 公司名称筛选（空字符串=全部）
    company_name: str = ""
    # 关键词模糊搜索（跨标题/摘要/正文/公司名，空字符串=不限）
    keyword: str = ""
    # 发布日期范围（YYYY-MM-DD，空字符串=不限）
    date_from: str = ""
    date_to: str = ""
    # 页码（从1开始）
    page: int = 1
    # 每页条数
    page_size: int = 50
