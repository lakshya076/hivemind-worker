import re
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

DEFAULT_AIDER_MODEL = "openrouter/openrouter/free"
DEFAULT_AGY_MODEL = "gemini-3.7-flash-medium"

# Model names end up on the command line. Real model identifiers only use these
# characters; anything else (spaces, quotes, &, |, ^, %) is rejected so it can
# never be interpreted by a shell or cmd.exe.
_MODEL_RE = re.compile(r"^[A-Za-z0-9._:/@+\-]{1,200}$")


class UnsupportedEngineError(ValueError):
    pass


@dataclass
class AgentCommand:
    """How to launch an agent.

    ``args`` never contain the prompt unless ``prompt_in_args`` is True; the
    prompt is delivered through ``stdin_file`` or a ``--message-file`` instead,
    so it reaches the agent intact (no newline truncation or length limit) and
    can never be parsed by a shell.
    """

    args: List[str]
    stdin_file: Optional[Path] = None
    prompt_in_args: bool = False


def validate_model(model: Optional[str]) -> Optional[str]:
    if model is None:
        return None
    if not _MODEL_RE.match(model):
        raise UnsupportedEngineError(f"Invalid model name {model!r}")
    return model


def build_agent_command(
    engine: str,
    prompt_file: Path,
    prompt_text: str,
    model: Optional[str] = None,
) -> AgentCommand:
    """Build the launch command for a headless agent session."""
    engine_key = engine.strip().lower()
    model = validate_model(model)

    if engine_key == "aider":
        return AgentCommand(args=[
            "aider",
            "--model", model or DEFAULT_AIDER_MODEL,
            "--message-file", str(prompt_file),
            "--yes-always",
            "--no-auto-commits",
            "--no-dirty-commits",
            "--no-show-model-warnings",
        ])

    if engine_key in ("claude-code", "claude"):
        # Headless `claude -p` cannot answer permission prompts, so without a
        # permission mode every file edit is denied. acceptEdits allows edits
        # (not arbitrary shell commands). The prompt is read from stdin.
        args = ["claude", "-p", "--permission-mode", "acceptEdits"]
        if model:
            args += ["--model", model]
        return AgentCommand(args=args, stdin_file=prompt_file)

    if engine_key == "agy":
        # agy's text print mode only takes the prompt as an argument. That is safe
        # as long as agy is a native executable launched without a shell; the
        # runner refuses batch-file shims for commands with prompt_in_args.
        return AgentCommand(
            args=["agy", "-p", prompt_text, "--model", model or DEFAULT_AGY_MODEL],
            prompt_in_args=True,
        )

    raise UnsupportedEngineError(
        f"Unsupported agent engine '{engine}'. Supported engines: claude-code, agy, aider."
    )
