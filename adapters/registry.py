"""Backend adapter registry — maps engine names to adapter classes."""

def get_backend_class(engine_name: str):
    if engine_name == "gemini-cli":
        from adapters.gemini_cli import GeminiCLIAdapter
        return GeminiCLIAdapter
    elif engine_name == "openrouter-api":
        from adapters.openrouter_api import OpenRouterAdapter
        return OpenRouterAdapter
    elif engine_name == "claude-cli":
        from adapters.claude_cli import ClaudeCLIAdapter
        return ClaudeCLIAdapter
    elif engine_name == "codex-cli":
        from adapters.codex_cli import CodexCLIAdapter
        return CodexCLIAdapter
    else:
        raise ValueError(f"Unknown engine: {engine_name}")
