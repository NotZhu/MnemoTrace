"""便捷工具：加载本地 .env，并根据友好的 provider 名称构建 LiteLLMClient/嵌入器。

API key 因此不进代码、也不进 git（.env 已被 gitignore）。评测框架会用到。"""
from __future__ import annotations

import os


def load_dotenv(path: str = ".env") -> None:
    """极简 .env 加载器（无依赖）。不会覆盖已设置的环境变量。"""
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key, val = key.strip(), val.strip().strip('"').strip("'")
            os.environ.setdefault(key, val)


def make_llm(name: str, **overrides):
    """根据友好名称构建 LiteLLMClient：

    deepseek | deepseek-reasoner | qwen-plus | qwen-turbo | qwen-max |
    univibe:<model> (OpenAI relay) | univibe-claude:<model> | <raw litellm model id>
    """
    from .litellm_llm import LiteLLMClient

    n = name.strip()
    if n in ("deepseek", "deepseek-chat"):
        return LiteLLMClient("deepseek/deepseek-chat", **overrides)
    if n == "deepseek-reasoner":
        return LiteLLMClient("deepseek/deepseek-reasoner", **overrides)
    if n.startswith("qwen"):
        return LiteLLMClient(
            f"openai/{n}",
            api_base=os.environ.get("ALI_BASE_URL"),
            api_key=os.environ.get("ALI_API_KEY") or os.environ.get("DASHSCOPE_API_KEY"),
            **overrides,
        )
    if n.startswith("univibe-claude:"):
        return LiteLLMClient(
            f"anthropic/{n.split(':', 1)[1]}",
            api_base=os.environ.get("UNIVIBE_ANTHROPIC_BASE"),
            api_key=os.environ.get("UNIVIBE_API_KEY"),
            **overrides,
        )
    if n.startswith("univibe:"):
        return LiteLLMClient(
            f"openai/{n.split(':', 1)[1]}",
            api_base=os.environ.get("UNIVIBE_OPENAI_BASE"),
            api_key=os.environ.get("UNIVIBE_API_KEY"),
            **overrides,
        )
    # 火山引擎 / 火山方舟 (ByteDance Ark) — OpenAI 兼容；模型可用名称或 endpoint id (ep-...)
    if n.startswith(("volcano:", "ark:", "doubao:")):
        return LiteLLMClient(
            f"openai/{n.split(':', 1)[1]}",
            api_base=os.environ.get("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3"),
            api_key=os.environ.get("ARK_API_KEY"),
            **overrides,
        )
    # Moonshot AI / Kimi — OpenAI 兼容。第三方记忆基准报告答案方用的就是同一个类（Kimi-K2.x），
    # 所以让我们的检索跑在上面才是 apples-to-apples、无星号备注的骨干。
    if n.startswith(("moonshot:", "kimi:")):
        overrides.setdefault("temperature", 1.0)  # Kimi-K2.x 拒绝任何 temperature != 1
        return LiteLLMClient(
            f"openai/{n.split(':', 1)[1]}",
            api_base=os.environ.get("MOONSHOT_BASE_URL", "https://api.moonshot.cn/v1"),
            api_key=os.environ.get("MOONSHOT_API_KEY"),
            **overrides,
        )
    return LiteLLMClient(n, **overrides)


def make_embedder(name: str = "lexical", **overrides):
    """lexical | fastembed | bge-small | bge-m3 | bge-large | multilingual | bge-small-zh
    | st:<model>（sentence-transformers）| openai:<model>（LiteLLM）。
    openai: 省略模型时使用 BAAI/bge-m3（1024 维）；其他 API 模型需显式传 dim。

    记忆内容含非英文文本时选 `multilingual`；bge-* 预设只支持英文。"""
    n = name.strip()
    if n == 'lexical':
        return None
    if n == 'fastembed':
        from ..embed import FastEmbedEmbedder

        return FastEmbedEmbedder(**overrides)
    presets = {
        "bge-small": "BAAI/bge-small-en-v1.5",
        "bge-large": "BAAI/bge-large-en-v1.5",
        "bge-m3": "BAAI/bge-m3",
        # 上面三个只支持英文，这对存储个人笔记的记忆是个坑：在 16 份真实中文记忆文档上实测，
        # bge-small-en 对全部三个查询都把同一份无关文档排在第 1（0/3），而 multilingual 拿到 3/3、
        # 分数差拉开 2.5 倍。bge-small-zh 得到 2/3。基准路径仍然显式传 --embedder bge-small
        # （LongMemEval 是英文），所以这些预设不影响任何已发布数字。
        "multilingual": "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2",
        "bge-small-zh": "BAAI/bge-small-zh-v1.5",
    }
    if n in presets or n.startswith("st:"):
        from ..embed import SentenceTransformerEmbedder

        model = presets.get(n) or n.split(":", 1)[1]
        return SentenceTransformerEmbedder(model, **overrides)
    if n.startswith("openai:"):
        from ..embed import LiteLLMEmbedder

        model = n.split(":", 1)[1]
        return LiteLLMEmbedder(model, **overrides) if model else LiteLLMEmbedder(**overrides)
    if "/" not in n:
        raise ValueError('Unknown embedder. Use lexical, fastembed, or an explicit model identifier.')
    from ..embed import SentenceTransformerEmbedder

    return SentenceTransformerEmbedder(n, **overrides)


def configured_embedder(name=None, *, default='lexical'):
    name = (name if name is not None else os.environ.get('MNEMOTRACE_EMBEDDER', default)).strip()
    embedder = make_embedder(name)
    return embedder, getattr(embedder, 'model_id', name) if embedder is not None else None


def make_reranker(name: str = "none", **overrides):
    """none/off -> None；bge-reranker | bge-reranker-large | bge-reranker-v2 | <原生 HF cross-encoder>。"""
    n = (name or "none").strip()
    if n in ("none", "off", ""):
        return None
    presets = {
        "bge-reranker": "BAAI/bge-reranker-base",
        "bge-reranker-large": "BAAI/bge-reranker-large",
        "bge-reranker-v2": "BAAI/bge-reranker-v2-m3",
    }
    from ..retrieve.rerank import CrossEncoderReranker

    return CrossEncoderReranker(presets.get(n, n), **overrides)
