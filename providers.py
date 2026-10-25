"""
=========================================================
多供应商配置（providers.py）
=========================================================
职责：定义支持的 LLM 供应商（全部 OpenAI 兼容接口），以及各家的
      默认 base_url / model。供应商的 API Key 等运行时配置存在
      settings 表（键 llm_providers），由 settings.py 管理。
=========================================================
设计：
  - 全部走 OpenAI 兼容接口（/chat/completions），openai SDK 换
    base_url + api_key + model 即可——llm_processor._get_client()
    已经是这么写的，因此多供应商接入对它零改动。
  - 豆包（火山方舟）特殊：model 字段填的是【推理接入点 ID ep-xxx】
    （在火山方舟控制台创建接入点后生成），不能直接填模型名，故默认空。
  - 当前使用哪家：settings 表键 llm_active_provider 控制（默认 deepseek）。
    切换后 load_runtime_settings() 会把 Config.DEEPSEEK_API_KEY /
    DEEPSEEK_BASE_URL / DEEPSEEK_MODEL 同步指向当前供应商，
    各调用点沿用这几个 Config 属性即可，不需要逐处改。
=========================================================
"""

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com",
        "model": "deepseek-chat",
        "tip": "",  # 无特殊提示
    },
    "qwen": {
        "label": "通义千问",
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "model": "qwen-plus",
        "tip": "",
    },
    "kimi": {
        "label": "Kimi",
        "base_url": "https://api.moonshot.cn/v1",
        "model": "moonshot-v1-32k",
        "tip": "",
    },
    "doubao": {
        "label": "豆包",
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
        "model": "",  # 豆包需填火山方舟「推理接入点」ID（ep-xxx），不能直接填模型名
        "tip": "模型需填火山方舟「推理接入点」ID（ep-xxx），可先在方舟控制台创建接入点",
    },
    "glm": {
        "label": "智谱GLM",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        "model": "glm-4-flash",
        "tip": "",
    },
}

# 默认供应商（首次启动 / 未配置时的兜底）
DEFAULT_PROVIDER = "deepseek"
