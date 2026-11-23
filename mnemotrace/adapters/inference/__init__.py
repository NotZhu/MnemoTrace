"""装配基于 OpenAI 兼容协议的补全和嵌入适配器。"""

from ._http import InferenceRequestError
from .config import InferenceClients, open_inference

__all__ = ["InferenceClients", "InferenceRequestError", "open_inference"]
