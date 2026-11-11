"""Default prompts shared by memory entry points."""
from .planning import SYSTEM as EXTRACT_SYSTEM
from .summarizer import SESSION_DIGEST_SYSTEM
from ..profile import SYSTEM as PROFILE_SYSTEM

POLICY_DEFAULTS = {
    "extract_instruction": "",
    "extract_system": EXTRACT_SYSTEM,
    "digest_system": SESSION_DIGEST_SYSTEM,
    "profile_system": PROFILE_SYSTEM,
}
