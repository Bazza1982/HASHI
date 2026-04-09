"""Light onboarding phase — runs at TUI startup before normal chat begins."""
from __future__ import annotations

import http.client
import json
import re
from pathlib import Path


def strip_ansi(text: str) -> str:
    text = re.sub(r'\x1b\[[0-9;]*m', '', text)
    text = re.sub(r'\{style\}|\{reset\}', '', text)
    return text


def load_languages(bridge_home: Path) -> list[dict]:
    lang_dir = bridge_home / "onboarding" / "languages"
    order = [
        "english.json", "japanese.json", "chinese_sim.json", "chinese_trad.json",
        "korean.json", "german.json", "french.json", "russian.json", "arabic.json",
    ]
    files = sorted(lang_dir.glob("*.json"), key=lambda x: order.index(x.name) if x.name in order else 99)
    langs = []
    for f in files:
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
            data["_file"] = f.name
            langs.append(data)
        except Exception:
            pass
    return langs


def lang_code_from_file(filename: str) -> str:
    mapping = {
        "chinese_sim": "zh", "chinese_trad": "zh-tw", "japanese": "ja",
        "korean": "ko", "german": "de", "french": "fr", "russian": "ru", "arabic": "ar",
    }
    for key, code in mapping.items():
        if key in filename.lower():
            return code
    return "en"


def get_disclaimer(bridge_home: Path, lang_code: str) -> str:
    p = bridge_home / "onboarding" / "languages" / f"disclaimer_{lang_code}.md"
    if not p.exists():
        p = bridge_home / "onboarding" / "languages" / "disclaimer_en.md"
    return p.read_text(encoding="utf-8") if p.exists() else "(Disclaimer not found)"


def get_wellbeing(lang: dict) -> str:
    raw = lang.get("continueToHatch", "")
    return strip_ansi(raw)


def _ping_openrouter(key: str) -> bool:
    try:
        conn = http.client.HTTPSConnection("openrouter.ai", timeout=8)
        conn.request("GET", "/api/v1/models", headers={"Authorization": f"Bearer {key}"})
        return conn.getresponse().status == 200
    except Exception:
        return False


def _ping_deepseek(key: str) -> bool:
    try:
        conn = http.client.HTTPSConnection("api.deepseek.com", timeout=8)
        conn.request("GET", "/models", headers={"Authorization": f"Bearer {key}"})
        return conn.getresponse().status == 200
    except Exception:
        return False


def detect_key_type(key: str) -> str:
    """Returns 'openrouter' or 'deepseek' based on key prefix."""
    return "openrouter" if key.startswith("sk-or-") else "deepseek"


def _read_secrets(bridge_home: Path) -> dict:
    p = bridge_home / "secrets.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _write_secrets(bridge_home: Path, secrets: dict):
    p = bridge_home / "secrets.json"
    p.write_text(json.dumps(secrets, indent=2), encoding="utf-8")


def check_existing_api_key(bridge_home: Path) -> tuple[bool, str]:
    """Check if a working API key already exists. Returns (ok, engine_name)."""
    secrets = _read_secrets(bridge_home)
    or_key = secrets.get("openrouter-api_key") or secrets.get("openrouter_key")
    ds_key = secrets.get("deepseek-api_key")
    if or_key and _ping_openrouter(or_key):
        return True, "openrouter-api"
    if ds_key and _ping_deepseek(ds_key):
        return True, "deepseek-api"
    return False, ""


def save_new_api_key(bridge_home: Path, key: str) -> tuple[bool, str]:
    """Validate and save a new API key. Returns (ok, engine_name)."""
    key_type = detect_key_type(key)
    ok = _ping_openrouter(key) if key_type == "openrouter" else _ping_deepseek(key)
    if not ok:
        return False, ""
    secrets = _read_secrets(bridge_home)
    if key_type == "openrouter":
        secrets["openrouter-api_key"] = key
        engine = "openrouter-api"
    else:
        secrets["deepseek-api_key"] = key
        engine = "deepseek-api"
    _write_secrets(bridge_home, secrets)
    return True, engine


def ensure_agents_json(bridge_home: Path, engine: str):
    """Generate agents.json from agents.json.sample if missing or empty — hashiko only."""
    agents_path = bridge_home / "agents.json"
    if agents_path.exists():
        try:
            cfg = json.loads(agents_path.read_text(encoding="utf-8"))
            if cfg.get("agents"):
                return
        except Exception:
            pass

    sample_path = bridge_home / "agents.json.sample"
    if not sample_path.exists():
        return

    sample = json.loads(sample_path.read_text(encoding="utf-8"))
    agents = [a for a in sample.get("agents", []) if a.get("name") == "hashiko"]
    if not agents:
        agents = sample.get("agents", [])[:1]

    for a in agents:
        a["engine"] = engine
        a["active_backend"] = engine
        a["is_active"] = True

    cfg = {
        "global": sample.get("global", {"authorized_id": 0}),
        "agents": agents,
    }
    agents_path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")


def write_completion_marker(bridge_home: Path, lang_code: str):
    workspace = bridge_home / "workspaces" / "hashiko"
    workspace.mkdir(parents=True, exist_ok=True)
    marker = workspace / "tui_onboarding_complete"
    marker.write_text(json.dumps({"lang": lang_code, "completed": True}), encoding="utf-8")


def is_onboarding_complete(bridge_home: Path) -> bool:
    return (bridge_home / "workspaces" / "hashiko" / "tui_onboarding_complete").exists()


def build_wakeup_prompt(lang_code: str, lang_name: str) -> str:
    prompts = {
        "zh": (
            "[SYSTEM: TUI 首次启动] 用户刚刚通过 TUI 终端完成了首次设置。"
            "用户选择的语言是：中文（简体）。"
            "请用中文热情地打招呼，介绍自己是 Hashiko（小乔），"
            "问用户叫什么名字、希望怎么称呼，把答案存到记忆里，"
            "然后一步一步引导用户设置 Telegram。"
        ),
        "zh-tw": (
            "[SYSTEM: TUI 首次啟動] 用戶剛剛通過 TUI 終端完成了首次設置。"
            "用戶選擇的語言是：中文（繁體）。"
            "請用繁體中文熱情地打招呼，介紹自己是 Hashiko（小喬），"
            "問用戶叫什麼名字、希望怎麼稱呼，把答案存到記憶裡，"
            "然後一步一步引導用戶設置 Telegram。"
        ),
        "ja": (
            "[SYSTEM: TUI初回起動] ユーザーがTUIターミナルで初回セットアップを完了しました。"
            "ユーザーが選択した言語は日本語です。"
            "日本語で温かく挨拶し、自分がHashiko（ハシコ）であることを紹介し、"
            "ユーザーの名前と呼び方を聞いて、メモリに保存し、"
            "その後、Telegramの設定を一歩ずつ案内してください。"
        ),
        "ko": (
            "[SYSTEM: TUI 첫 실행] 사용자가 TUI 터미널에서 초기 설정을 완료했습니다. "
            "사용자가 선택한 언어는 한국어입니다. "
            "한국어로 따뜻하게 인사하고, 자신이 Hashiko(하시코)임을 소개하고, "
            "사용자의 이름과 호칭을 물어서 메모리에 저장한 후, "
            "Telegram 설정을 단계별로 안내해 주세요."
        ),
        "de": (
            "[SYSTEM: TUI ERSTSTART] Der Benutzer hat gerade die Ersteinrichtung über das TUI-Terminal abgeschlossen. "
            "Die gewählte Sprache ist Deutsch. "
            "Bitte begrüße den Benutzer herzlich auf Deutsch, stelle dich als Hashiko vor, "
            "frage nach dem Namen und der bevorzugten Anrede, speichere es im Gedächtnis, "
            "und leite dann Schritt für Schritt die Telegram-Einrichtung an."
        ),
        "fr": (
            "[SYSTEM: TUI PREMIER LANCEMENT] L'utilisateur vient de terminer la configuration initiale via le terminal TUI. "
            "La langue choisie est le français. "
            "Veuillez saluer chaleureusement l'utilisateur en français, vous présenter en tant que Hashiko, "
            "demander son nom et comment il souhaite être appelé, sauvegarder en mémoire, "
            "puis guider pas à pas la configuration de Telegram."
        ),
        "ru": (
            "[SYSTEM: TUI ПЕРВЫЙ ЗАПУСК] Пользователь только что завершил первоначальную настройку через терминал TUI. "
            "Выбранный язык — русский. "
            "Пожалуйста, тепло поприветствуйте пользователя на русском, представьтесь как Hashiko, "
            "спросите имя и предпочтительное обращение, сохраните в память, "
            "затем пошагово помогите настроить Telegram."
        ),
        "ar": (
            "[SYSTEM: TUI أول تشغيل] أكمل المستخدم للتو الإعداد الأولي عبر واجهة TUI. "
            "اللغة المختارة هي العربية. "
            "يرجى الترحيب بالمستخدم بحرارة باللغة العربية، وتقديم نفسك باسم Hashiko، "
            "واسأل عن اسمه وكيف يفضل أن يُنادى، واحفظ ذلك في الذاكرة، "
            "ثم قم بتوجيهه خطوة بخطوة لإعداد Telegram."
        ),
    }
    if lang_code in prompts:
        return prompts[lang_code]
    return (
        "[SYSTEM: TUI FIRST RUN] The user has just completed first-time setup via the TUI terminal. "
        "Their selected language is English. "
        "Please greet them warmly in English, introduce yourself as Hashiko, "
        "ask for their name and how they'd like to be addressed, save it to memory, "
        "then guide them through Telegram setup — one step at a time."
    )


class LightOnboardingPhase:
    """State machine for TUI_onboarding first-run flow."""

    PHASE_LANG = "lang"
    PHASE_DISCLAIMER = "disclaimer"
    PHASE_WELLBEING = "wellbeing"
    PHASE_APICHECK = "apicheck"       # auto-runs (no input), triggers worker thread
    PHASE_APIKEY_INPUT = "apikey_input"  # shown only if no valid key found
    PHASE_DONE = "done"

    def __init__(self, bridge_home: Path):
        self.bridge_home = bridge_home
        self.phase = self.PHASE_LANG
        self.langs = load_languages(bridge_home)
        self.selected_lang: dict | None = None
        self.l_code = "en"
        self.engine = "openrouter-api"

    def get_initial_prompt(self) -> str:
        lines = [
            "🌍 HASHI — Welcome / ようこそ / 欢迎",
            "",
            "Select your language / 言語を選択 / 选择语言:",
        ]
        for i, lang in enumerate(self.langs, 1):
            lines.append(f"  [{i}] {lang.get('displayName', f'Language {i}')}")
        lines.append("\nEnter number:")
        return "\n".join(lines)

    def handle_input(self, text: str) -> tuple[str, bool]:
        """Process user input. Returns (message_to_display, needs_more_input).
        If needs_more_input is False and phase is APICHECK, caller should run worker."""

        if self.phase == self.PHASE_LANG:
            try:
                idx = int(text.strip()) - 1
                if 0 <= idx < len(self.langs):
                    self.selected_lang = self.langs[idx]
                    self.l_code = lang_code_from_file(self.selected_lang.get("_file", ""))
                    disclaimer = get_disclaimer(self.bridge_home, self.l_code)
                    self.phase = self.PHASE_DISCLAIMER
                    return (
                        f"✅ Language: {self.selected_lang.get('displayName', '?')}\n\n"
                        + disclaimer
                        + "\n\nPress Enter to continue:"
                    ), True
                return "Invalid number. Try again:", True
            except ValueError:
                return "Please enter a number:", True

        if self.phase == self.PHASE_DISCLAIMER:
            wellbeing = get_wellbeing(self.selected_lang or {})
            self.phase = self.PHASE_WELLBEING
            if wellbeing:
                return "✅ Noted.\n\n" + wellbeing + "\n\nPress Enter to continue:", True
            # No wellbeing text — skip straight to API check
            self.phase = self.PHASE_APICHECK
            return "✅ Noted.\n\n🔍 Checking API connectivity...", False

        if self.phase == self.PHASE_WELLBEING:
            self.phase = self.PHASE_APICHECK
            return "✅\n\n🔍 Checking API connectivity...", False  # triggers worker

        if self.phase == self.PHASE_APIKEY_INPUT:
            key = text.strip()
            if not key:
                return "Key cannot be empty. Please paste your OpenRouter or DeepSeek API key:", True
            ok, engine = save_new_api_key(self.bridge_home, key)
            if ok:
                self.engine = engine
                self.phase = self.PHASE_DONE
                return f"✅ API key verified ({engine})! Setting up...", False
            key_type = detect_key_type(key)
            return f"❌ Key not working ({key_type}). Please try again:", True

        return "", False

    def run_api_check(self) -> tuple[str, bool]:
        """Blocking check — run in worker thread. Returns (message, key_found)."""
        ok, engine = check_existing_api_key(self.bridge_home)
        if ok:
            self.engine = engine
            self.phase = self.PHASE_DONE
            return f"✅ API connection confirmed ({engine}).", True
        self.phase = self.PHASE_APIKEY_INPUT
        return (
            "❌ No active API key found.\n\n"
            "Please paste your OpenRouter key (sk-or-v1-...) "
            "or DeepSeek key (sk-...):"
        ), False

    def finalize(self):
        """Write completion marker and ensure agents.json exists."""
        ensure_agents_json(self.bridge_home, self.engine)
        write_completion_marker(self.bridge_home, self.l_code)

    def get_wakeup_prompt(self) -> str:
        lang_name = (self.selected_lang or {}).get("displayName", "English")
        return build_wakeup_prompt(self.l_code, lang_name)
