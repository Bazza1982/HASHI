"""Backend adapter registry — maps engine names to adapter classes."""

def get_backend_class(engine_name: str):
    if engine_name == "gemini-cli":
        from adapters.gemini_cli import GeminiCLIAdapter
        return GeminiCLIAdapter
    elif engine_name == "openrouter-api":
        from adapters.openrouter_api import OpenRouterAdapter
        return OpenRouterAdapter
    elif engine_name == "deepseek-api":
        from adapters.deepseek_api import DeepSeekAdapter
        return DeepSeekAdapter
    elif engine_name == "claude-cli":
        from adapters.claude_cli import ClaudeCLIAdapter
        return ClaudeCLIAdapter
    elif engine_name == "codex-cli":
        from adapters.codex_cli import CodexCLIAdapter
        return CodexCLIAdapter
    elif engine_name == "claw-cli":
        from adapters.claw_cli import ClawCLIAdapter
        return ClawCLIAdapter
    elif engine_name == "grok-cli":
        from adapters.grok_cli import GrokCLIAdapter
        return GrokCLIAdapter
    elif engine_name == "ollama-api":
        from adapters.ollama_api import OllamaAdapter
        return OllamaAdapter
    elif engine_name == "xai-api":
        from adapters.xai_api import XaiApiAdapter
        return XaiApiAdapter
    else:
        raise ValueError(f"Unknown engine: {engine_name}")
