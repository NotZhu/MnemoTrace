"""忆痕 —— 开源长期记忆引擎。项目章程见 CLAUDE.md。

Memory facade 接好后，公共 API 在本模块定稿（见 mnemotrace/memory.py）。
"""

__version__ = "0.1.0"

from .config import Config
from .types import Entity, Episode, Fact, Relation

__all__ = ["__version__", "Config", "Episode", "Fact", "Entity", "Relation"]

# Memory facade 延迟挂载以避免导入开销；见 mnemotrace/memory.py。
try:  # pragma: no cover - exercised once memory.py lands
    from .memory import Memory  # noqa: F401

    __all__.append("Memory")
except Exception:  # noqa: BLE001 - 开发期部分构建的容错
    pass
