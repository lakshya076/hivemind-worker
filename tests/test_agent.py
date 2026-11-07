from pathlib import Path

import pytest

from hivemind_worker.agent import (
    DEFAULT_AGY_MODEL,
    DEFAULT_AIDER_MODEL,
    UnsupportedEngineError,
    build_agent_command,
)

PROMPT_FILE = Path("prompt.txt")
PROMPT = "line one\nline \"two\" & | ^ %PATH%"


def test_aider_reads_prompt_from_file():
    cmd = build_agent_command("aider", PROMPT_FILE, PROMPT)
    assert cmd.args[0] == "aider"
    assert cmd.args[cmd.args.index("--message-file") + 1] == str(PROMPT_FILE)
    assert cmd.args[cmd.args.index("--model") + 1] == DEFAULT_AIDER_MODEL
    assert "--no-auto-commits" in cmd.args
    assert PROMPT not in cmd.args and not cmd.prompt_in_args


@pytest.mark.parametrize(
    "flag",
    # With --yes-always aider would otherwise self-upgrade via pip (and exit without
    # doing the task), fetch URLs from the prompt, edit .gitignore, and opt in to analytics.
    ["--no-check-update", "--no-detect-urls", "--no-gitignore", "--no-analytics", "--no-show-release-notes"],
)
def test_aider_never_auto_accepts_dangerous_prompts(flag):
    assert flag in build_agent_command("aider", PROMPT_FILE, PROMPT).args


@pytest.mark.parametrize("engine", ["claude-code", "claude", " Claude-Code "])
def test_claude_reads_prompt_from_stdin_and_can_edit(engine):
    cmd = build_agent_command(engine, PROMPT_FILE, PROMPT, model="sonnet")
    assert cmd.args[:4] == ["claude", "-p", "--permission-mode", "acceptEdits"]
    assert cmd.args[-2:] == ["--model", "sonnet"]
    assert cmd.stdin_file == PROMPT_FILE
    assert PROMPT not in cmd.args


def test_claude_without_model_passes_no_model_flag():
    assert "--model" not in build_agent_command("claude", PROMPT_FILE, PROMPT).args


def test_agy_passes_prompt_as_argument():
    cmd = build_agent_command("agy", PROMPT_FILE, PROMPT)
    assert cmd.args == ["agy", "-p", PROMPT, "--model", DEFAULT_AGY_MODEL]
    assert cmd.prompt_in_args


def test_unknown_engine_rejected():
    with pytest.raises(UnsupportedEngineError):
        build_agent_command("bash", PROMPT_FILE, PROMPT)


@pytest.mark.parametrize("model", ["--dangerous", "a b", "x;rm -rf", "m&calc", "a\"b", ""])
def test_model_injection_rejected(model):
    with pytest.raises(UnsupportedEngineError):
        build_agent_command("aider", PROMPT_FILE, PROMPT, model=model)


@pytest.mark.parametrize("model", ["openrouter/anthropic/claude-sonnet", "gpt-4o@2024", "gemini-3.7-flash:latest"])
def test_real_model_names_accepted(model):
    cmd = build_agent_command("aider", PROMPT_FILE, PROMPT, model=model)
    assert model in cmd.args
