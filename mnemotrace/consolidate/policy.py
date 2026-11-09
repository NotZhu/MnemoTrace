"""Default prompts shared by memory entry points."""
from .planning import SYSTEM as EXTRACT_SYSTEM
from .summarizer import PERSONA_SYSTEM, SESSION_DIGEST_SYSTEM

POLICY_DEFAULTS = {
    "extract_instruction": "",
    "extract_system": EXTRACT_SYSTEM,
    "digest_system": SESSION_DIGEST_SYSTEM,
    "persona_system": PERSONA_SYSTEM,
}
