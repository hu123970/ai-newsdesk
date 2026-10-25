"""
=========================================================
提示词模板模块（prompts.py）— Stage 3 实现
=========================================================
职责：管理所有发给 DeepSeek LLM 的提示词（prompt）模板。
=========================================================
学习注释：
  提示词工程（Prompt Engineering）是 LLM 应用的核心。
  好的提示词 = 清晰的角色定义 + 具体的任务说明 + 结构化输出格式 + 示例。

  这里我们把系统提示词（System Prompt）和用户提示词（User Prompt）分开：
  - System Prompt：定义 AI 的角色、能力边界和输出格式
  - User Prompt：给出具体要处理的文章内容

  这样做的好处：
  1. System Prompt 可以很长（角色设定），User Prompt 聚焦在具体内容
  2. 方便调试——改角色不用改内容，改内容不用改角色
  3. 符合 DeepSeek API 的 Chat Completion 格式
=========================================================
"""

# ==================== 监控公司列表（运行时动态维护） ====================
# 公司列表保存在数据库 settings 表的 target_companies 键中，由 settings.py 管理。
# 前端设置弹窗可以增删公司，保存后调用 prompts.reload_prompts() 重建：
#   TARGET_COMPANIES   —— LLM 提示词里的公司列表文本块（按领域/地区分组）
#   COMPANY_KEYWORDS   —— 采集预筛选的关键词列表
#   SYSTEM_PROMPT / BATCH_SYSTEM_PROMPT —— LLM 系统提示词
# import 时先用下面的 DEFAULT_COMPANIES 初始化，保证模块可独立使用。

# 默认种子（首次启动写进数据库）——从原硬编码列表转录。
# 每家公司：
#   name     —— 显示/输出名（简称）
#   region   —— 所属地区：china（国内）| overseas（海外）
#   category —— 所属领域：ai_model | data_labeling | embodied_ai
#   keywords —— 别名/关键词（用于预筛选匹配，name 本身自动包含，不用重复写）
DEFAULT_COMPANIES = [
    # ====== AI 大模型 ======
    {"name": "OpenAI", "region": "overseas", "category": "ai_model", "keywords": ["ChatGPT"]},
    {"name": "Anthropic", "region": "overseas", "category": "ai_model", "keywords": ["Claude"]},
    {"name": "Google", "region": "overseas", "category": "ai_model", "keywords": ["DeepMind", "Gemini"]},
    {"name": "Meta", "region": "overseas", "category": "ai_model", "keywords": ["FAIR", "Llama"]},
    {"name": "Microsoft", "region": "overseas", "category": "ai_model", "keywords": ["Copilot"]},
    {"name": "xAI", "region": "overseas", "category": "ai_model", "keywords": ["Elon Musk", "Grok"]},
    {"name": "Perplexity AI", "region": "overseas", "category": "ai_model", "keywords": ["Perplexity"]},
    {"name": "NVIDIA", "region": "overseas", "category": "ai_model", "keywords": ["英伟达"]},
    {"name": "阿里", "region": "china", "category": "ai_model", "keywords": ["阿里巴巴", "通义", "Qwen"]},
    {"name": "腾讯", "region": "china", "category": "ai_model", "keywords": ["混元"]},
    {"name": "字节跳动", "region": "china", "category": "ai_model", "keywords": ["字节", "豆包"]},
    {"name": "月之暗面", "region": "china", "category": "ai_model", "keywords": ["Kimi"]},
    {"name": "智谱AI", "region": "china", "category": "ai_model", "keywords": ["智谱", "GLM", "ChatGLM"]},
    {"name": "DeepSeek", "region": "china", "category": "ai_model", "keywords": ["深度求索"]},
    {"name": "百度", "region": "china", "category": "ai_model", "keywords": ["文心", "Ernie"]},
    {"name": "MiniMax", "region": "china", "category": "ai_model", "keywords": []},
    {"name": "科大讯飞", "region": "china", "category": "ai_model", "keywords": ["星火"]},
    {"name": "商汤科技", "region": "china", "category": "ai_model", "keywords": ["商汤"]},
    {"name": "零一万物", "region": "china", "category": "ai_model", "keywords": []},
    {"name": "阶跃星辰", "region": "china", "category": "ai_model", "keywords": []},
    {"name": "昆仑万维", "region": "china", "category": "ai_model", "keywords": ["天工"]},
    # ====== 数据标注 ======
    {"name": "Scale AI", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "Surge AI", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "Mercor", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "Innodata", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "Labelbox", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "Appen", "region": "overseas", "category": "data_labeling", "keywords": []},
    {"name": "海天瑞声", "region": "china", "category": "data_labeling", "keywords": []},
    {"name": "云测数据", "region": "china", "category": "data_labeling", "keywords": ["云测"]},
    {"name": "数据堂", "region": "china", "category": "data_labeling", "keywords": []},
    {"name": "标贝科技", "region": "china", "category": "data_labeling", "keywords": ["标贝"]},
    {"name": "每日互动", "region": "china", "category": "data_labeling", "keywords": []},
    {"name": "本原数据", "region": "china", "category": "data_labeling", "keywords": []},
    # ====== 具身智能 / 机器人 ======
    {"name": "特斯拉", "region": "overseas", "category": "embodied_ai", "keywords": ["Tesla"]},
    {"name": "银河通用", "region": "china", "category": "embodied_ai", "keywords": []},
    {"name": "智元机器人", "region": "china", "category": "embodied_ai", "keywords": ["智元"]},
    {"name": "宇树科技", "region": "china", "category": "embodied_ai", "keywords": ["宇树", "Unitree"]},
    {"name": "优必选", "region": "china", "category": "embodied_ai", "keywords": ["UBTECH"]},
    {"name": "松延动力", "region": "china", "category": "embodied_ai", "keywords": []},
    {"name": "星海图", "region": "china", "category": "embodied_ai", "keywords": []},
]

# 领域中文标签（用于把公司列表组织成 LLM 提示词文本块）
_CATEGORY_LABELS = {
    "ai_model": "AI大模型",
    "data_labeling": "数据标注",
    "embodied_ai": "具身智能",
}


def build_target_companies_block(companies: list) -> str:
    """按 领域 → 海外/国内 分组，生成 LLM 提示词里的公司列表文本块。"""
    blocks = []
    for cat_key, cat_label in _CATEGORY_LABELS.items():
        cat_companies = [c for c in companies if c.get("category") == cat_key]
        if not cat_companies:
            continue
        lines = [f"【{cat_label}领域】"]
        for region_label, region_value in (("海外", "overseas"), ("国内", "china")):
            group = [c for c in cat_companies if c.get("region") == region_value]
            if not group:
                continue
            parts = []
            for c in group:
                display = c["name"]
                # 别名放到括号里作为注释，帮助 LLM 识别（如"阿里（通义千问/Qwen）"）
                aliases = [k for k in c.get("keywords") or [] if k]
                if aliases:
                    display = f"{display}（{'/'.join(aliases)}）"
                parts.append(display)
            lines.append(f"{region_label}：" + "、".join(parts))
        blocks.append("\n".join(lines))
    if not blocks:
        return "（暂无监控公司）"
    return "\n\n".join(blocks)


def build_company_keywords(companies: list) -> list:
    """把公司列表展平成预筛选关键词（公司名 + 别名），去重保序。"""
    seen, result = set(), []#set表示不重复的集合
    for c in companies:
        for k in [c.get("name") or ""] + list(c.get("keywords") or []):
            k = k.strip()
            if k and k not in seen:
                seen.add(k)
                result.append(k)
    return result


# 运行时当前监控公司列表（内存镜像，与数据库保持一致）
_companies: list = []


def reload_prompts(companies: list):
    """
    用最新的监控公司列表重建全部提示词常量。

    学习注释：
      settings.py 在启动加载和保存公司列表时调用。
      重建四个常量，让预筛选（COMPANY_KEYWORDS）和 LLM 分析
      （SYSTEM_PROMPT/BATCH_SYSTEM_PROMPT）都立即吃到新列表。
    """
    global _companies, TARGET_COMPANIES, COMPANY_KEYWORDS, SYSTEM_PROMPT, BATCH_SYSTEM_PROMPT
    _companies = companies or []
    TARGET_COMPANIES = build_target_companies_block(_companies)
    COMPANY_KEYWORDS = build_company_keywords(_companies)
    role = _ANALYST_ROLE_TEMPLATE.format(target_companies=TARGET_COMPANIES)
    SYSTEM_PROMPT = role + "\n\n" + _SINGLE_OUTPUT_FORMAT
    BATCH_SYSTEM_PROMPT = role + "\n\n" + _BATCH_OUTPUT_FORMAT


def get_current_companies() -> list:
    """返回当前监控公司列表（供前端设置弹窗读取）。"""
    return list(_companies)

# ==================== 共享分析角色模板（System Prompt 底座） ====================
# 单篇 / 批量模式共用的角色、任务、三大领域、判断标准、公司提取、标题摘要要求。
# 只有"输出格式"部分不同，所以拆成模板 + 各自的输出格式段再拼接。
# {target_companies} 由 reload_prompts 用当前监控公司列表填充（因此这里不是 f-string）。

_ANALYST_ROLE_TEMPLATE = """你是一个专业的 AI 行业信息分析助手。你精通中英文，深刻理解人工智能行业的技术和商业动态。

## 你的任务
对给定的文章内容进行分析，判断其是否属于我们关注的领域，并提取关键信息。

## 关注的三大领域
1. **AI大模型（ai_model）**：大语言模型（LLM）、多模态模型、AI平台与服务、AI应用与工具、AI安全与对齐、GPU/算力/芯片、开源模型
2. **数据标注（data_labeling）**：AI训练数据标注服务、数据标注工具与平台、合成数据生成、RLHF/人类反馈、数据质量与治理
3. **具身智能（embodied_ai）**：人形机器人、具身智能体、机器人操作系统、运动控制/灵巧手、自动驾驶（端到端模型）、传感器与感知

> 注：GPU/算力/芯片、机器人零部件等属于上述领域，但**仅当文章涉及目标公司时才算相关**（见下方硬规则）。

## 判断标准
- **相关**：内容直接涉及上述领域的技术突破、产品发布、商业合作、融资、政策或行业趋势，且至少涉及一家目标公司
- **不相关**：内容仅泛泛提及 AI 但核心不是 AI（如某公司用 AI 做了个营销活动）、纯消费电子（如新款手机发布，除非强调 AI 功能）、不涉及 AI 的常规企业新闻、或虽然涉及 AI 但没有提到任何目标公司
- ⚠️ **硬规则**：判定「相关」的**唯一标准**是——文章是否涉及至少一家【目标公司】。文章属于 AI 领域（包括 GPU/算力/芯片、机器人等）但没提到任何目标公司 → **is_relevant 一律为 false**，宁可漏判不可误判
- ⚠️ **摘要纪律**：ai_summary 永远只陈述事实（这篇文章讲了什么），**绝对不要写**「未提及目标公司」「不判定为相关」「故不相关」「判断为不相关」「判定为不相关」这类判定性评语。当 is_relevant=false 时，写一句客观摘要即可，不要解释"为什么不算相关"

## 涉及公司提取
- 严格从以下目标公司列表中匹配，**不要添加列表之外的公司**：
{target_companies}
- ⚠️ 只提取上述列表中的公司。列表外的公司一律忽略——即使该公司也做 AI（如 360、小米、高通、华为等不在列表中，绝对不能提取）
- 如果一个目标公司都没涉及，返回空字符串
- 多个公司用中文顿号（、）分隔
- **公司名只输出简称**：列表中括号里的内容是注释（如中文名、产品名），帮助你识别公司，但输出时只取括号前面的简称。例如：列表写"DeepSeek（深度求索）"→ 输出"DeepSeek"，列表写"阿里（通义千问）"→ 输出"阿里"，列表写"xAI (Elon Musk)"→ 输出"xAI"
- ⚠️ 如果文章提到的公司都不在目标列表中，即使内容是 AI 相关，is_relevant 也应设为 false（因为我们只关心目标公司的动态）

## 公司地区判定
- 每条相关新闻都要输出 region 字段，按**公司所属地区**判定，与信源/媒体地区无关：
  - 中国公司（如 阿里、腾讯、字节跳动、DeepSeek、月之暗面、宇树科技、海天瑞声）→ "china"
  - 海外公司（如 OpenAI、Google、Microsoft、Meta、NVIDIA、Scale AI、特斯拉）→ "overseas"
- 例如：36氪 报道 OpenAI 的新闻 → region 应为 "overseas"，TechCrunch 报道阿里 → region 应为 "china"
- 涉及多家公司且地区不一致时，以文章最主要的公司（或占主导的公司）的地区为准

## 标题和摘要生成要求
- 标题（ai_title）：10-25 字的中文短标题，抓核心事件，不要"震惊"、"重磅"等标题党用语
- 摘要（ai_summary）：2-3 句中文摘要（80-200 字），说清楚"谁做了什么、意味着什么"
"""

# ==================== 单篇输出格式 ====================

_SINGLE_OUTPUT_FORMAT = """## 输出格式
你必须严格按照以下 JSON 格式输出，不要输出任何 JSON 之外的内容：
```json
{
  "is_relevant": true,
  "category": "ai_model",
  "company_name": "OpenAI、微软",
  "region": "overseas",
  "ai_title": "OpenAI 发布 GPT-5 预览版",
  "ai_summary": "OpenAI 今日发布了 GPT-5 预览版，在推理能力和多模态理解方面实现重大突破。新模型在 MMLU 基准测试中达到 92.5 分，较 GPT-4 提升 15%。此举将进一步加剧大模型赛道的竞争。"
}
```

字段说明：
- is_relevant: true（相关）或 false（不相关）
- category: "ai_model"、"data_labeling"、"embodied_ai" 三选一（不相关时为空字符串）
- company_name: 涉及的公司名，多个用中文顿号分隔（不相关时为空字符串）
- region: 按公司所属地区判定，"china"（国内公司）或 "overseas"（海外公司），与信源地区无关（不相关时为空字符串）
- ai_title: 你生成的中文标题（不相关时为空字符串）
- ai_summary: 你生成的中文摘要（不相关时为空字符串）
"""

# ==================== 批量输出格式 ====================

_BATCH_OUTPUT_FORMAT = """## 输出格式
本次请求包含多篇文章（已编号）。你必须严格按照以下 JSON 数组格式输出，不要输出任何数组之外的内容：
```json
[
  {"is_relevant": true, "category": "ai_model", "company_name": "OpenAI、微软", "region": "overseas", "ai_title": "OpenAI 发布 GPT-5 预览版", "ai_summary": "OpenAI 今日发布了 GPT-5 预览版……"},
  {"is_relevant": false, "category": "", "company_name": "", "region": "", "ai_title": "", "ai_summary": ""}
]
```

规则：
- 数组元素数量必须等于本次提供的文章数量，顺序与【文章 1】、【文章 2】…一一对应
- **每篇文章（包括不相关的）都必须输出一个对象**：不相关时 is_relevant 为 false，其余字段为空字符串
- 不要省略任何文章，不要合并多篇文章，不要返回 null
- 禁止输出数组之外的任何文字

字段说明（与单篇模式相同）：
- is_relevant: true（相关）或 false（不相关）
- category: "ai_model"、"data_labeling"、"embodied_ai" 三选一（不相关时为空字符串）
- company_name: 涉及的公司名，多个用中文顿号分隔（不相关时为空字符串）
- region: 按公司所属地区判定，"china"（国内公司）或 "overseas"（海外公司），与信源地区无关（不相关时为空字符串）
- ai_title: 你生成的中文标题（不相关时为空字符串）
- ai_summary: 你生成的中文摘要（不相关时为空字符串）
"""

# SYSTEM_PROMPT / BATCH_SYSTEM_PROMPT 不再在这里直接定义，
# 而是由 reload_prompts() 用当前监控公司列表动态重建（见文件顶部），
# 模块 import 时用 DEFAULT_COMPANIES 初始化一次（见文件底部）。

# ==================== 用户提示词模板（User Prompt） ====================
# 用 Python 的 format 方法填充 {title} 和 {content}

USER_PROMPT_TEMPLATE = """请分析以下 AI 行业相关文章：

【标题】
{title}

【来源】
{source_name}

【正文/摘要】
{content}

请按照系统提示中的 JSON 格式输出分析结果。"""

# ==================== 批量用户提示词模板 ====================
# 多篇文章打包进一次调用。逐篇用 BATCH_USER_PROMPT_ARTICLE 拼块，
# 首尾各一个固定模板，明确要求按编号顺序输出 JSON 数组。

BATCH_USER_PROMPT_HEADER = "请一次分析以下 {count} 篇 AI 行业相关文章，每篇已编号，编号顺序很重要：\n\n"

BATCH_USER_PROMPT_ARTICLE = """【文章 {index}】
标题：{title}
来源：{source_name}
正文/摘要：{content}
"""

BATCH_USER_PROMPT_FOOTER = "\n请严格按照系统提示中的批量 JSON 数组格式输出，数组元素顺序必须与【文章 1】到【文章 {count}】的编号一一对应，每篇都要有一个对象。不要输出数组之外的任何内容。"

# ==================== 关键词预筛选（第一道防线） ====================
# 在调用 LLM 之前，先用关键词快速判断文章是否可能相关。
# 如果标题和内容中完全没有任何目标公司或 AI 关键词，直接丢弃，省 LLM 费用。

# COMPANY_KEYWORDS（目标公司关键词）不再硬编码，
# 由 reload_prompts() 根据当前监控公司列表（含别名）动态重建，
# quick_filter 运行时读取该模块常量，自动吃到最新列表。

# ==================== 关键词预筛选：强信号 / 弱泛词分级（2026-08-06 优化） ====================
# 背景：原 AI_KEYWORDS 是"命中任一即放行"的单级列表，其中大量泛词
#   （agent / robot / sensor / safety / training / 推理 / 参数 / 芯片…）在泛科技媒体
#   几乎篇篇出现，导致 54 条采集里 50+ 条无关文章被送进 LLM 白烧 token。
# 现在拆成两级：
#   STRONG_SIGNAL_KEYWORDS —— 命中即送 LLM（这些词单独出现时，文章大概率就是三大领域内容）
#   WEAK_KEYWORDS          —— 只命中这些词时不放行（泛词，命中≠AI 相关）
# 判据与 LLM 判定标准对齐：LLM 判定"必须涉及至少一家目标公司"才算相关，
# 所以"只命中弱泛词、不含目标公司"的文章送进去也必被判不相关，提前挡掉不产生漏报。

# 强信号词：单独出现即大概率属于 AI大模型 / 数据标注 / 具身智能 三大领域
STRONG_SIGNAL_KEYWORDS = [
    # ====== AI大模型 — 英文 ======
    "large language model", "LLM", "foundation model", "multimodal",
    "generative AI", "GenAI", "RAG", "RLHF", "reinforcement learning",
    "pre-training", "post-training", "distillation", "mixture of experts",
    "MoE", "diffusion", "transformer", "attention mechanism",
    "alignment", "red teaming", "jailbreak", "hallucination",
    "superintelligence", "AGI", "ASI", "GPT",
    "text-to-image", "text-to-video", "image generation", "video generation",
    "speech recognition", "speech synthesis", "TTS",
    "open source model", "fine-tuning", "AlphaFold",
    # ====== AI大模型 — 中文 ======
    "大模型", "大语言模型", "基础模型", "多模态", "生成式", "AIGC",
    "预训练", "强化学习", "蒸馏", "混合专家", "注意力机制",
    "自然语言处理", "语音识别", "语音合成", "文生图", "文生视频", "图生视频",
    "开源模型", "微调", "智能体", "对齐", "幻觉",
    # ====== 数据标注 — 英文 ======
    "data labeling", "data annotation", "synthetic data",
    "human annotator", "human feedback", "instruction tuning",
    "supervised fine-tuning", "SFT", "data curation",
    # ====== 数据标注 — 中文 ======
    "数据标注", "标注平台", "标注工具", "合成数据", "指令微调",
    "人类反馈", "数据治理", "数据闭环",
    # ====== 具身智能 — 英文 ======
    "embodied", "humanoid", "bipedal", "locomotion", "grasping", "dexterous",
    "autonomous driving", "end-to-end driving", "self-driving",
    "motor control",
    # ====== 具身智能 — 中文 ======
    "具身智能", "人形机器人", "自动驾驶", "灵巧手", "运动控制",
    "双足", "抓取",
    # ====== 算力/芯片（三大领域共用基础设施） ======
    "GPU", "TPU", "NPU", "H100", "H200", "B100", "B200", "A100", "算力",
]

# 弱泛词：泛科技/商业新闻里几乎篇篇出现，命中≠AI 相关，不单独放行。
# 保留在这里仅作"已知噪声"参考，将来想微调预筛强度时可从这里搬词回强信号。
WEAK_KEYWORDS = [
    # AI 泛词：营销套话，绝大多数不涉及目标公司
    "artificial intelligence", "machine learning", "deep learning",
    "neural network", "人工智能", "机器学习", "深度学习", "神经网络",
    # 歧义词：英文 agent 指经纪人/代理，token 指代币
    "agent", "token",
    # 产品发布会必提的泛词
    "benchmark", "safety", "training", "推理", "参数", "上下文", "基准测试",
    # 通用硬件/行业词：工厂/手机/家电/汽车都提
    "robot", "robotics", "机器人", "sensor", "chip", "传感器", "芯片",
    "manipulation", "操作", "导航", "路径规划", "感知", "驱动器", "关节",
    # 数据行业泛词：不一定是 AI 标注
    "data quality", "data pipeline", "数据集", "数据质量",
    # IT 基础设施泛词
    "数据中心", "云计算", "服务器",
]

def quick_filter(title: str, content: str) -> bool:
    """
    快速关键词预筛选。

    返回 True 表示可能相关（需要送 LLM 进一步判断），
    返回 False 表示确定不相关（可以直接丢弃）。

    判断逻辑（分级，2026-08-06 优化）：
    1. 标题或内容中提到任一目标公司 → 通过（最高优先级，公司动态一条不漏）
    2. 标题或内容中提到任一强信号关键词 → 通过（大概率是三大领域内容）
    3. 以上都没有（含只命中弱泛词如 agent/robot/safety）→ 不通过
       （弱泛词在泛科技媒体篇篇出现，命中≠AI 相关；
         且 LLM 判定标准是"必须涉及目标公司"，这类文章送进去也必被拒，
         提前挡掉省 token、不产生漏报）
    """
    text = f"{title} {content}".lower()

    for kw in COMPANY_KEYWORDS:
        if kw.lower() in text:
            return True

    for kw in STRONG_SIGNAL_KEYWORDS:
        if kw.lower() in text:
            return True

    return False


# ==================== 模块初始化 ====================
# import 时先用默认种子重建全部提示词常量，
# 保证 prompts 模块可独立使用；启动后 settings.load_runtime_settings()
# 会用数据库里的最新监控公司列表再重建一次。
reload_prompts(DEFAULT_COMPANIES)

# ==================== 每日日报提示词 ====================
# 日报 = 当日新闻 + 跨时间联想（公司历史 + 往期日报回放）。
# {category_label} 领域中文名；{news_block}/{history_block}/{prev_reports_block}
# 由 daily_report.py 组装成文本块后 format 进来。

REPORT_SYSTEM_PROMPT = """你是一名资深的 AI 行业研究分析师，为管理层撰写每日 AI 行业动态日报。

## 你关注的领域
- AI大模型（ai_model）：大语言模型、多模态、AI平台与应用、AI安全、算力/芯片
- 数据标注（data_labeling）：标注服务与平台、合成数据、RLHF/人类反馈、数据质量与治理
- 具身智能（embodied_ai）：人形机器人、具身智能体、运动控制/灵巧手、自动驾驶（端到端）、传感器

## 今日新闻素材（{category_label}领域）
{news_block}

## 历史上下文（用于跨时间联想）
### 1. 相关公司近期的历史动态（可能与本日事件相关）
{history_block}

### 2. 该领域最近几期日报的摘要与趋势观察
{prev_reports_block}

## 写作要求
1. 从行业研究专业分析师角度筛选信息，宁缺毋滥：只保留对行业有实质影响的动态，避免罗列式流水账
2. 结构五块齐全，每块都必须有内容：
   - **summary（核心结论）**：一段话，点出当天最重要的 2-4 件事和整体判断
   - **events（重点事件）**：按重要性排序，每项 = 事件标题 + 一句话说明它的影响
   - **impact（影响分析）**：拆成 2-4 个要点，每个要点 = 一个小标题（2-8字，如「行业格局」「产业链」「竞争态势」）+ 1-2 句阐述——这些事件对行业格局/产业链/竞争态势分别意味着什么
   - **strategy（战略意义）**：拆成 2-4 个要点，每个要点 = 一个小标题（如「投资启示」「企业战略」「政策方向」）+ 1-2 句阐述——对投资、企业战略、政策方向的启示
   - **trend（趋势观察）**：必须结合【历史上下文】做跨时间联想，拆成 2-4 个要点，每个要点 = 一个小标题（如「量变到质变」「共性趋势」）+ 1-2 句阐述——识别同一公司或同一主题的持续性动作、共性趋势、从量变到质变的信号
3. 句式精炼，信息要全、句子要短，避免空话套话和冗余展开；小标题要精炼有力，不要写成一句长话
4. 若某领域当日没有值得写的事件，在 summary 里如实说明，不硬凑"""


REPORT_OUTPUT_FORMAT = """## 输出格式
必须严格按照以下 JSON 输出，不要输出任何 JSON 之外的内容：
```json
{
  "title": "如「AI大模型 · 8月10日：多家头部厂商竞逐多模态，算力军备升级」",
  "summary": "核心结论（一段话，200-300字）",
  "events": [
    {"title": "事件一标题", "impact": "事件说明与影响（一两句）"},
    {"title": "事件二标题", "impact": "事件说明与影响（一两句）"}
  ],
  "impact": [
    {"point": "小标题（如「行业格局」）", "detail": "1-2句阐述"},
    {"point": "小标题", "detail": "1-2句阐述"}
  ],
  "strategy": [
    {"point": "小标题（如「投资启示」）", "detail": "1-2句阐述"},
    {"point": "小标题", "detail": "1-2句阐述"}
  ],
  "trend": [
    {"point": "小标题（如「量变到质变」）", "detail": "1-2句阐述"},
    {"point": "小标题", "detail": "1-2句阐述"}
  ]
}
```"""


def build_daily_report_prompt(category_label: str, date: str, news_block: str,
                              history_block: str, prev_reports_block: str) -> tuple:
    """
    组装日报的 (system_prompt, user_prompt)。
    传入的都是已格式化的文本块（空时用占位文案，避免 format 报错）。
    """
    sys_prompt = REPORT_SYSTEM_PROMPT.format(
        category_label=category_label,
        news_block=news_block or "（今日该领域无相关新闻）",
        history_block=history_block or "（暂无历史上下文）",
        prev_reports_block=prev_reports_block or "（暂无往期日报）",
    ) + "\n\n" + REPORT_OUTPUT_FORMAT
    user_prompt = f"今天是 {date}（上海时间）。请撰写「{category_label}」领域的每日日报，严格按系统提示的 JSON 格式输出。"
    return sys_prompt, user_prompt


# ==================== RAG 问答提示词 ====================
# 基于库中新闻与日报回答用户问题，引用必须标注 [参考资料 N]。

QA_KEYWORD_EXTRACT_PROMPT = """从下面这个问题中提取 3-8 个检索关键词，用于在 AI 行业新闻库中检索。
要求：
- 优先用 ≥3 字的中文词（如"人形机器人""深度求索"）或完整的英文单词（如"OpenAI""GPU"）
- 2 字中文词（如"宇树"）也可以，但尽量与其它词混用
- 输出严格为 JSON 数组，不要输出其它内容：["关键词1","关键词2",...]

问题：{question}"""


QA_SYSTEM_PROMPT = """你是一个 AI 行业情报问答助手，资料库覆盖「AI 行业动态聚合」库中的新闻与每日日报。

回答规则：
1. 必须基于下方【参考资料】回答，不得编造或臆测资料之外的事实
2. 涉及资料内容时，在对应句子末尾用 [参考资料 N] 标注（N 为参考资料的编号）
3. 资料不足时，明确说明"库内暂无相关信息"或"资料不足以回答该问题"，不要强行作答
4. 用中文回答，按逻辑适当分段（每段空一行），句式精炼
5. 禁止使用任何 Markdown 标记：不要用 *、**、#、- 等符号做加粗/标题/列表，一律输出纯文本"""


def build_qa_answer_prompt(question: str, context: str) -> tuple:
    """组装 RAG 答案生成的 (system_prompt, user_prompt)。context 为已编号的参考资料文本。"""
    user_prompt = f"## 问题\n{question}\n\n## 参考资料\n{context or '（库中没有检索到任何相关参考资料）'}\n\n请给出回答。"
    return QA_SYSTEM_PROMPT, user_prompt
