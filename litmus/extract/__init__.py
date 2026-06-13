"""WS-B extraction (DESIGN §11, §19): a PDF -> schema-valid ClaimGraph via Opus 4.8.

The model transcribes and locates; it never judges (DESIGN §3.1). The public entry point is
``extract_claim_graph``; ``extract_to_file`` adds the canonical JSON cache (DESIGN §10).
"""

from __future__ import annotations

from .extractor import (
    DEFAULT_CLAIMS_DIR,
    DEFAULT_MODEL,
    ExtractionError,
    build_claim_graph,
    default_out_path,
    default_paper_id,
    extract_claim_graph,
    extract_to_file,
)
from .prompts import EXTRACTION_SYSTEM_PROMPT

__all__ = [
    "extract_claim_graph",
    "extract_to_file",
    "build_claim_graph",
    "default_paper_id",
    "default_out_path",
    "EXTRACTION_SYSTEM_PROMPT",
    "DEFAULT_MODEL",
    "DEFAULT_CLAIMS_DIR",
    "ExtractionError",
]
