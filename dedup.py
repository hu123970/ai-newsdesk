"""
=========================================================
新闻查重模块（dedup.py）
=========================================================
职责：检测"同一事件的多源报道"（近重复），供推送前合并卡片。
=========================================================
背景：
  采集去重只在 URL 层（source_url UNIQUE）。不同媒体报同一事件的
  多个链接会全部入库、全部被 LLM 标记相关、同批全部推送。
  用户痛点：一次钉钉推送里好几张卡片讲同一件事。

  关键抓手：LLM 生成的 ai_title 是 prompt 强制的"10-25 字中文短标题"。
  同一事件的多源报道，LLM 独立生成的中文标题会高度相似——
  甚至跨语言（Verge 英文原文与钛媒体中文报道都会得到
  "OpenAI 发布 GPT-5.6-Cyber" 这种中文标题）。
  所以按 ai_title 的相似度聚类，是最可靠的查重信号。

相似度算法（基于真实数据实测选型，2026-08-11）：
  单一 Jaccard / Overlap 都不够稳——标题是"意译改写"时字符重合很低
  （"灵巧手赛道半年融资250亿" vs "灵巧手融资半年破250亿"，
  Jaccard 仅 0.29，单靠它必然漏判）。
  单一阈值也不够稳——两个不同事件可能和两个同事件拥有几乎相同的
  序列相似度（实测：宇树"定价引热议"vs"创业之路"=0.424，
  Meta"30B Muse Glimmer"vs"Glimmer愿景"=0.423，几乎持平），
  必须看它们共享的"关键令牌"类型才能分开。

  最终采用三级闸门：
    ① sim ≥ HIGH（0.70）                    → 直接判同事件
    ② sim ≥ LOW_STRONG（0.40）且共享强令牌   → 判同事件
       （强令牌 = 英文词≥3字母 / 数字≥2位，如 glimmer、250）
    ③ sim ≥ LOW_CJK（0.55）且共享中文令牌    → 判同事件
       （中文令牌 = 去泛词后的中文二元组；弱信号，需更高相似度，
         防止人名/泛词把两个不同事件带进同一簇）

  强令牌可信度高（模型名/金额数字很少撞车），低相似度就能定；
  中文令牌里有人名地名（如"王兴兴"），要更高相似度才敢并。
  真实样本分离效果（40 条相关新闻）：同事件 7 对全部合并、
  不同事件 7 对全部不并，其中"看似同相似度、实为不同类型"的两对
  （0.424 vs 0.423）靠令牌类型正确分开。

聚类方式：贪心 + 与簇首个元素比对（防链式过度合并）。
  如果 A~B、B~C 但 A!~C，B、C 不会通过 B 硬挤进 A 的簇，
  避免把"同一公司连续两条相关但不同的动态"误并成一张卡片。
=========================================================
"""

import re
from difflib import SequenceMatcher

# ---------- 文本归一化 ----------
# 只保留汉字 / 小写英数，去掉标点、空格、emoji。

_HAN = "\\u4e00-\\u9fff"
_NON_TEXT = re.compile(f"[^{_HAN}a-z0-9]+")

# 英文词 / 数字关键令牌
_LATIN_TOKEN = re.compile(r"[a-z][a-z0-9.\-]+")
_NUM_TOKEN = re.compile(r"\d{2,}")

# 中文"泛词"二元组：科技新闻标题里到处出现，当不了"同一事件"的区分信号。
# 如"发布""模型""技术"在毫不相干的新闻里都出现，必须从中文令牌里滤掉。
_GENERIC_CJK = set("""
发布 推出 宣布 正式 全新 新一代 完成 打造 升级 布局 进军 发力 押注 亮相 上线
模型 技术 产品 平台 系统 领域 行业 公司 市场 用户 全球 中国 美国 世界 国内 海外
智能 数据 应用 场景 能力 时代 未来 方向 发展 进展 动态 首家 首个 首次 领先
""".split())


def normalize(text: str) -> str:
    """归一化：小写、去标点空格、仅留汉字英数。"""
    return _NON_TEXT.sub("", (text or "").lower())


def _strip_latin(text: str) -> set:
    """提取 ≥3 字母的英文词（内嵌 . 和 -，如 gpt-5.6-cyber）。"""
    out = set()
    for m in _LATIN_TOKEN.finditer((text or "").lower()):
        w = m.group(0).strip(".-")
        if len(w) >= 3:
            out.add(w)
    return out


def _strip_num(text: str) -> set:
    """提取 ≥2 位数字（如 250 / 30 / 70）。"""
    return set(_NUM_TOKEN.findall(text or ""))


def _strip_cjk(text: str, company: str = "") -> set:
    """提取中文二元组（去泛词、去公司名自身），供弱信号闸门用。"""
    out = set()
    norm = normalize(text)
    for i in range(len(norm) - 1):
        bg = norm[i:i + 2]
        if bg in _GENERIC_CJK:
            continue
        out.add(bg)
    if company:
        cnorm = normalize(company)
        for i in range(len(cnorm) - 1):
            out.discard(cnorm[i:i + 2])
    return out


def strong_tokens(text: str) -> set:
    """强令牌：英文词（≥3 字母）+ 数字（≥2 位）。"""
    return _strip_latin(text) | _strip_num(text)


def key_tokens(text: str, company: str = "") -> dict:
    """
    提取标题的关键令牌，按强度分成两组：
      {"strong": {英文词≥3字母, 数字≥2位}, "cjk": {中文二元组(去泛词)}}
    公司名自身的令牌会被剔除（公司是分组键，组内人人共有，不算区分信号）。
    """
    return {
        "strong": strong_tokens(text) - _strip_latin(company),
        "cjk": _strip_cjk(text, company),
    }


def seq_similarity(a: str, b: str) -> float:
    """序列相似度：difflib 最长公共子序列比（0~1）。对意译改写鲁棒。"""
    na, nb = normalize(a), normalize(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _gate(sim: float, ta: dict, tb: dict, high=0.70, low_strong=0.40, low_cjk=0.55) -> bool:
    """三级闸门：相似度 + 共享令牌类型综合判定是否同一事件。"""
    if sim >= high:
        return True
    if sim >= low_strong and (ta["strong"] & tb["strong"]):
        return True
    if sim >= low_cjk and (ta["cjk"] & tb["cjk"]):
        return True
    return False


def cluster_near_duplicates(
    items: list,
    text_fn,
    tokens_fn=None,
    high: float = 0.70,
    low_strong: float = 0.40,
    low_cjk: float = 0.55,
) -> list:
    """
    把"同一事件"的条目聚成簇。

    参数：
      items: 待聚类的条目列表（dict）
      text_fn: 取比较文本的函数（如 lambda x: x["ai_title"]）
      tokens_fn: 取关键令牌字典的函数
                 （如 lambda x: key_tokens(x["ai_title"], x["company_name"])）；
                 缺省用 key_tokens(text_fn(item))（不排除公司名）
      high / low_strong / low_cjk: 三级闸门阈值

    返回：
      list[list]，每个内层 list 是一簇（首元素为该簇锚点）。
      不相似的元素各自成簇（长度 1）。
    """
    if tokens_fn is None:
        tokens_fn = lambda it: key_tokens(text_fn(it))

    clusters: list = []
    for item in items:
        text = text_fn(item) or ""
        if not normalize(text):
            clusters.append([item])
            continue
        toks = tokens_fn(item)
        placed = False
        for cl in clusters:
            # 优先与簇锚点（首个元素）走完整三级闸门；
            # 闸门不过时，再与簇内任意成员走"高相似直连"（≥HIGH）——
            # 因为事件的多源报道可能存在"意译跨度"（A~B 0.6、B~C 0.83、A~C 0.46），
            # 只跟锚点比会把该并的漏掉。直连要求 ≥HIGH，链式过度合并
            # （A~B 0.42、B~C 0.42 逐级蹭上来）过不了 0.70，安全。
            anchor = cl[0]
            if _gate(seq_similarity(text, text_fn(anchor)), toks, tokens_fn(anchor),
                     high, low_strong, low_cjk):
                cl.append(item)
                placed = True
                break
            if any(seq_similarity(text, text_fn(m)) >= high for m in cl):
                cl.append(item)
                placed = True
                break
        if not placed:
            clusters.append([item])
    return clusters


# ---------- 测试入口 ----------

if __name__ == "__main__":
    """
    直接运行 python dedup.py 验证聚类效果。
    用例来自真实库最近 40 条相关新闻（同事件/不同事件各 7 对）。
    """
    raw_cases = [
        # ---- 同事件（应合并） ----
        [("灵巧手赛道半年融资250亿，标准尚未确立", ""), ("灵巧手融资半年破250亿，行业仍无标准手", ""), True],
        [("OpenAI推出网络安全AI模型", "OpenAI"), ("OpenAI发布GPT-5.6-Cyber网络安全模型", "OpenAI"), True],
        [("OpenAI发布GPT-5.6-Cyber网络安全模型", "OpenAI"), ("OpenAI发布GPT-5.6-Cyber助力安全防御", "OpenAI"), True],
        [("Anthropic数学突破，Meta开源Muse Glimmer", "Meta"), ("Meta 发布开源 30B 参数 Muse Glimmer 模型", "Meta"), True],
        [("Meta 发布开源 30B 参数 Muse Glimmer 模型", "Meta"), ("Meta 新模型 Glimmer 展现扎克伯格个人智能愿景", "Meta"), True],
        [("阿里云数据中心交付周期缩短至100天", "阿里"), ("阿里云数据中心交付周期缩短至100天", "阿里"), True],
        [("即梦AI独家技术合作百花奖AIGC推优单元", ""), ("百花奖联合即梦AI设立AIGC推优单元", ""), True],
        # ---- 不同事件（不应合并） ----
        [("OpenAI完成70亿美元员工要约收购", "OpenAI"), ("OpenAI推出网络安全AI模型", "OpenAI"), False],
        [("OpenAI 浏览器 Atlas 停服，仅存活292天", "OpenAI"), ("OpenAI推出网络安全AI模型", "OpenAI"), False],
        [("宇树科技定价引热议，王兴兴低调劝退炒作", "宇树科技"), ("宇树科技开启申购，美团系重仓持股", "宇树科技"), False],
        [("宇树科技定价引热议，王兴兴低调劝退炒作", "宇树科技"), ("宇树科技创始人王兴兴的创业之路", "宇树科技"), False],
        [("办公Agent竞争升温，大厂抢占电梯间广告", "字节跳动"), ("豆包推荐酒店拿佣金，意图经济兴起", "字节跳动"), False],
        [("英伟达推5000亿美元算力融资，GPU成可投资资产", "NVIDIA"), ("OpenAI完成70亿美元员工要约收购", "OpenAI"), False],
        [("中国AI大模型上演适应辐射，四家各领风骚", ""), ("张一鸣与梁文锋AI路线之争", ""), False],
    ]

    print("=" * 70)
    print("三级闸门验证（sim≥0.70 | sim≥0.40+强令牌 | sim≥0.55+中文令牌）")
    print("=" * 70)
    miss = 0
    for (t1, c1), (t2, c2), same in raw_cases:
        sim = seq_similarity(t1, t2)
        ta, tb = key_tokens(t1, c1), key_tokens(t2, c2)
        shared_strong = ta["strong"] & tb["strong"]
        shared_cjk = ta["cjk"] & tb["cjk"]
        merged = _gate(sim, ta, tb)
        ok = (merged == same)
        if not ok:
            miss += 1
        verdict = "✓" if ok else "✗ 判反了"
        tag = "同事件" if same else "不同事件"
        reason = []
        if sim >= 0.70:
            reason.append("高相似")
        if sim >= 0.40 and shared_strong:
            reason.append(f"强令牌{sorted(shared_strong)[:2]}")
        if sim >= 0.55 and shared_cjk:
            reason.append(f"中令牌{sorted(shared_cjk)[:3]}")
        print(f"{verdict} [{tag}] sim={sim:.3f}  {'+'.join(reason) if reason else '—'} → 合并={merged}")
        if not ok:
            print(f"     {t1}")
            print(f"     {t2}")

    print("=" * 70)
    print(f"验证结果：{'全部正确' if miss == 0 else f'{miss} 对判错'}")

    # ---- 链式聚类：同一事件三源报道，标题意译跨度大，必须三合一 ----
    # A"推出网络安全AI模型" ~ B"发布GPT-5.6-Cyber网络安全模型" sim 0.60
    # B ~ C"发布GPT-5.6-Cyber助力安全防御" sim 0.83；A ~ C 仅 0.46（锚点闸门会拒）
    # 靠"与簇内任意成员 ≥HIGH 直连"（C~B 0.83）把 C 收进簇。
    print()
    print("=" * 70)
    print("链式聚类测试（OpenAI 网络安全模型三源报道）")
    chain = [
        {"ai_title": "OpenAI推出网络安全AI模型", "company_name": "OpenAI", "source_name": "TechCrunch"},
        {"ai_title": "OpenAI发布GPT-5.6-Cyber网络安全模型", "company_name": "OpenAI", "source_name": "VentureBeat"},
        {"ai_title": "OpenAI发布GPT-5.6-Cyber助力安全防御", "company_name": "OpenAI", "source_name": "The Decoder"},
    ]
    clusters = cluster_near_duplicates(
        chain, lambda x: x["ai_title"],
        tokens_fn=lambda x: key_tokens(x["ai_title"], x["company_name"]),
    )
    ok = (len(clusters) == 1 and len(clusters[0]) == 3)
    print(f"结果：{'✓ 三源合并为一张卡片' if ok else f'✗ 期望 1 簇 3 条，实际 {len(clusters)} 簇'}")
    for cl in clusters:
        print("  簇：", " | ".join(f"{m['source_name']}({m['ai_title']})" for m in cl))
