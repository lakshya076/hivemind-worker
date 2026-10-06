import os
import platform
from pathlib import Path
from typing import List, Optional

DEFAULT_AIDER_MODEL = "openrouter/openrouter/free"
DEFAULT_AGY_MODEL = "gemini-3.7-flash-medium"


class UnsupportedEngineError(ValueError):
    pass


def build_agent_args(
    engine: str,
    prompt_text: str,
    model: Optional[str] = None
) -> List[str]:
    """
    Build cross-platform argv list for launching headless agent sessions.
    Works natively on both Linux and Windows without shell quotation bugs.
    """
    engine_key = engine.strip().lower()

    if engine_key == "aider":
        chosen_model = model or DEFAULT_AIDER_MODEL
        return [
            "aider",
            "--model", chosen_model,
            "--message", prompt_text,
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-show-model-warnings"
        ]

    if engine_key in ("claude-code", "claude"):
        return ["claude", "-p", prompt_text]

    if engine_key == "agy":
        chosen_model = model or DEFAULT_AGY_MODEL
        return ["agy", "-p", prompt_text, "--model", chosen_model]

    raise UnsupportedEngineError(
        f"Unsupported agent engine '{engine}'. Supported engines: claude-code, agy, aider."
    )
