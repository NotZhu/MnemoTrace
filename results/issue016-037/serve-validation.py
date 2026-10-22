"""Isolated, offline engineering server; never uses the user's memory directory."""
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.update({
    "MNEMOTRACE_DATA_DIR": str(ROOT / ".mnemotrace" / "issue016-037-validation"),
    "MNEMOTRACE_API_KEYS": "synthetic-a:synthetic-console-a,synthetic-b:synthetic-console-b",
    "MNEMOTRACE_SPACES": "", "MNEMOTRACE_OPEN": "0", "MNEMOTRACE_ALLOW_ANONYMOUS": "0",
    "MNEMOTRACE_EMBEDDER": "hashing", "MNEMOTRACE_LLM": "", "MNEMOTRACE_ANSWERER": "",
    "MNEMOTRACE_VISION_LLM": "", "MNEMOTRACE_SESSION_OUTCOMES": "0",
    "MNEMOTRACE_CONFLICT_DETECTION": "0",
})
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("mnemotrace.server.app:app", host="127.0.0.1", port=8000, access_log=False)
