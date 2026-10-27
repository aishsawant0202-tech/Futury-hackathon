"""Central settings. Every other module reads paths, keys and model names from here."""
import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")


def _path(env_name: str, default: str) -> Path:
    p = Path(os.getenv(env_name, default))
    return p if p.is_absolute() else ROOT / p


LLM_BASE_URL = os.getenv("LLM_BASE_URL", "https://hip-gw.hessian.ai/v1")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
# German rulebook reading and extraction
LLM_MODEL_GERMAN = os.getenv("LLM_MODEL_GERMAN", "vllm/Kolibri-1")
# Writing checks (code) and agent reasoning
LLM_MODEL_CODE = os.getenv("LLM_MODEL_CODE", "vllm/GLM-5.3-Flash")

SPEC_DIR = _path("SPEC_DIR", "data/specs")
SAMPLE_DIR = _path("SAMPLE_DIR", "data/samples")
# The main spec; rules cite it by this name
SPEC_DOCUMENT = "Verfahrensbeschreibung DTA EEL 13.4"
SPEC_PDF = SPEC_DIR / "2026_03_10_Endfassung_Verfahrensbeschreibung_DTA_EEL_Version_13.4_ab_01.01.2027.pdf"
CACHE_DIR = ROOT / ".cache"
DB_PATH = ROOT / "specpilot.db"
