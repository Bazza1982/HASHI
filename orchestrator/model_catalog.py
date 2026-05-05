from __future__ import annotations

AVAILABLE_GEMINI_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-2.5-flash-lite",
]

AVAILABLE_OPENROUTER_MODELS = [
    "deepseek/deepseek-v3.2-exp",
    "moonshotai/kimi-k2.5",
    "google/gemini-3.1-flash-lite-preview",
]

AVAILABLE_CLAUDE_MODELS = [
    "claude-opus-4-7",
    "claude-sonnet-4-6",
    "claude-opus-4-6",
    "claude-haiku-4-5",
]

CLAUDE_MODEL_ALIASES = {
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-7",
    "haiku": "claude-haiku-4-5",
}

AVAILABLE_CLAUDE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]

AVAILABLE_CODEX_MODELS = [
    "gpt-5.5",
    "gpt-5.3-codex",
    "gpt-5.4",
    "gpt-5.2-codex",
    "gpt-5.1-codex-max",
    "gpt-5.2",
    "gpt-5.1-codex-mini",
]

AVAILABLE_CODEX_EFFORTS = ["low", "medium", "high", "xhigh"]
