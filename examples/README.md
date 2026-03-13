# HASHI Configuration Examples

This directory contains example configuration files for HASHI.

---

## Configuration Files

### `agents.json.example`
Basic agent configuration example. Copy to `../agents.json` and customize.

**Usage:**
```bash
cp examples/agents.json.example agents.json
# Edit agents.json with your settings
```

**Key fields:**
- `name`: Agent identifier
- `engine`: Backend engine (`gemini-cli`, `claude-cli`, `codex-cli`, `openrouter-api`)
- `authorized_id`: Your Telegram user ID
- `system_prompt_file`: Path to agent personality file

---

### `agents.json.linux.example`
Agent configuration optimized for Linux environments.

Differences from the basic example:
- Adjusted script paths for Linux
- WSL-compatible settings

---

### `agents.json.samples`
Collection of sample agent configurations with different personalities and backends.

Contains examples for:
- Multiple agents with different backends
- Flexible agents (switchable backends)
- Specialized task agents (coding, research, etc.)

---

### `agent_capabilities.json.example`
Agent-to-agent communication permissions.

Copy to `../agent_capabilities.json` to enable inter-agent messaging.

**Usage:**
```bash
cp examples/agent_capabilities.json.example agent_capabilities.json
# Edit to configure which agents can talk to each other
```

**Fields:**
- `can_talk_to`: List of agents this agent can send messages to
- `can_receive_from`: List of agents that can send to this agent
- `allowed_intents`: Permitted message types (`ask`, `notify`, `command`)
- `granted_scopes`: Permission scopes granted to this agent

---

### `secrets.json.sample`
Template for API keys and authentication tokens.

**Usage:**
```bash
cp examples/secrets.json.sample secrets.json
# Edit secrets.json with your actual keys
```

**⚠️ Important:** Never commit `secrets.json` to version control!

**Fields:**
- `telegram_bot_token`: Your Telegram bot token from [@BotFather](https://t.me/botfather)
- `openrouter_api_key`: OpenRouter API key (if using OpenRouter backend)
- `[agent_name]`: Agent-specific tokens (usually `WORKBENCH_ONLY_NO_TOKEN` for CLI backends)

---

### `tasks.json.example`
Scheduled tasks (heartbeats and cron jobs) configuration.

Copy to `../tasks.json` to enable automated agent tasks.

**Usage:**
```bash
cp examples/tasks.json.example tasks.json
# Edit to configure your scheduled tasks
```

**Task types:**
- **Heartbeats**: Periodic checks (interval-based)
  - `interval_seconds`: How often to run
  - `prompt`: What to ask the agent
  
- **Cron jobs**: Time-scheduled tasks
  - `time`: When to run (HH:MM format)
  - `prompt`: What to ask the agent

---

## Quick Start

To set up a new HASHI instance:

```bash
# 1. Copy configuration examples
cp examples/agents.json.example agents.json
cp examples/secrets.json.sample secrets.json
cp examples/tasks.json.example tasks.json

# 2. Edit agents.json
#    - Set your Telegram user ID in authorized_id
#    - Choose your backend (gemini-cli, claude-cli, codex-cli, or openrouter-api)
#    - Customize agent name and personality file

# 3. Edit secrets.json
#    - Add your Telegram bot token
#    - Add OpenRouter API key (if using OpenRouter)

# 4. (Optional) Edit tasks.json
#    - Configure periodic checks or scheduled tasks

# 5. Run onboarding for guided setup
python onboarding/onboarding_main.py

# 6. Or launch directly
python main.py
```

---

## Getting Tokens

### Telegram Bot Token
1. Open Telegram and search for [@BotFather](https://t.me/botfather)
2. Send `/newbot` and follow instructions
3. Copy the token (format: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)
4. Paste into `secrets.json`

### Telegram User ID
1. Search for [@userinfobot](https://t.me/userinfobot) in Telegram
2. Send `/start`
3. Copy your user ID (numeric, e.g., `123456789`)
4. Paste into `agents.json` → `authorized_id`

### OpenRouter API Key
1. Visit [openrouter.ai](https://openrouter.ai/)
2. Sign up and go to API Keys
3. Create a new key (format: `sk-or-v1-...`)
4. Paste into `secrets.json` → `openrouter_api_key`

---

## See Also

- [Installation Guide](../INSTALL.md)
- [Main Documentation](../README.md)
- [Skills System](../docs/SKILLS_SYSTEM_DESIGN.md)
