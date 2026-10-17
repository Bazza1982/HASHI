"""Backend-owned /call menus using the existing command interaction contract."""

import copy
import json
from dataclasses import dataclass
from html import escape
from pathlib import Path
from tempfile import TemporaryDirectory
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from orchestrator import runtime_session, ui_language
from orchestrator.compute_services import ComputeServiceError
from orchestrator.command_ui import (
    setting_card,
    selected_label,
    back_label,
    refresh_label,
)
from .config import CallConfig
from .compute_voice_catalog import (
    discover_compute_voice_directory,
)
from .contract import CallError
from .voice_catalog import voice_label
from .voice_previews import get_call_preview, preview_caption


@dataclass(frozen=True)
class VoicePreviewDelivery:
    profile_id: str
    assets: tuple
    caption: str
    payload: bytes | None = None
    renderer: str = "call_catalog"
    media_type: str = ""
    filename: str = ""

    @property
    def available(self):
        return bool(self.assets or self.payload)


class CallSettings:
    @staticmethod
    def choice(values, index):
        if not str(index).isdigit() or not 0 <= int(index) < len(values):
            raise CallError("call_menu_invalid")
        return values[int(index)]

    def __init__(self, runtime):
        self.runtime = runtime
        root = (
            getattr(runtime.global_config, "bridge_home", None)
            or runtime.global_config.project_root
        )
        self.root = Path(root)
        self.config = CallConfig(self.root / "call_profiles.json")
        self.config.initialize()
        self.owner, self.agent = runtime_session.owner_id(runtime), runtime.name
        self.voice_directory = None
        self.voice_directory_error = ""

    async def prepare_voice_directory(self):
        """Load a matching live Compute catalogue without changing Call state."""

        doc, targets = self.config.read()
        slot = self.config.profile(self.owner, self.agent, doc, targets).get("tts")
        if not slot:
            return None
        target = targets[slot["target_id"]]
        try:
            self.voice_directory = await discover_compute_voice_directory(
                self.root,
                target,
                secret_resolver=getattr(
                    self.runtime,
                    "connector_secret_resolver",
                    None,
                ),
            )
            self.voice_directory_error = ""
        except ComputeServiceError as exc:
            self.voice_directory = None
            self.voice_directory_error = exc.code
        return self.voice_directory

    def _directory_for(self, target):
        directory = self.voice_directory
        return (
            directory
            if directory is not None and directory.target_id == target.get("id")
            else None
        )

    @staticmethod
    def _language_order():
        locale = ui_language.current_locale().casefold()
        return (
            ("zh", "en", "ja", "fr")
            if locale.startswith("zh")
            else ("en", "zh", "ja", "fr")
        )

    @staticmethod
    def _language_label(code, fallback=""):
        normalized = str(code or "").casefold()
        if normalized in {"zh", "en", "ja", "fr"}:
            return ui_language.tr("call.voice.language." + normalized)
        return fallback or normalized or ui_language.tr("call.voice.language.unknown")

    @staticmethod
    def _gender_label(gender):
        normalized = str(gender or "unknown").casefold()
        if normalized not in {"female", "male", "mixed", "neutral", "nonbinary"}:
            normalized = "unknown"
        return ui_language.tr("call.voice.gender." + normalized)

    def _voice_groups(self, directory):
        groups = {}
        for voice in directory.catalogue.voices:
            groups.setdefault((voice.language, voice.gender), []).append(voice)
        language_order = {
            language: index for index, language in enumerate(self._language_order())
        }
        gender_order = {
            "female": 0,
            "male": 1,
            "mixed": 2,
            "neutral": 3,
            "nonbinary": 4,
            None: 5,
        }
        ordered = sorted(
            groups.items(),
            key=lambda item: (
                language_order.get(item[0][0], 99),
                item[0][0],
                gender_order.get(item[0][1], 98),
            ),
        )
        return tuple(
            (
                key,
                tuple(
                    sorted(voices, key=lambda voice: (voice.label.casefold(), voice.id))
                ),
            )
            for key, voices in ordered
        )

    def _voice_page(self, directory, voice_id):
        for index, (_key, voices) in enumerate(self._voice_groups(directory)):
            if any(voice.id == voice_id for voice in voices):
                return f"voice.{index}"
        raise CallError("call_voice_unavailable", 409)

    def _group_label(self, key, count=None, language_name=""):
        language, gender = key
        label = " · ".join(
            (
                self._language_label(language, language_name),
                self._gender_label(gender),
            )
        )
        return label if count is None else f"{label} · {count}"

    @staticmethod
    def _voice_option_label(voice):
        description = voice.description or ui_language.tr(
            "call.voice.description.unavailable"
        )
        if len(description) > 48:
            description = description[:47].rstrip() + "…"
        return f"{voice.label} · {description}"

    def _voice_facts(self, directory, voice, group=None):
        tr = ui_language.tr
        catalogue = directory.catalogue
        facts = [
            "<b>"
            + escape(tr("call.voice.catalogue"))
            + "</b> · Hashi Compute · "
            + escape(
                tr(
                    "call.voice.catalogue.count",
                    total=catalogue.total,
                )
            )
        ]
        if group is not None:
            language_name = next(
                (
                    item.language_name
                    for item in catalogue.voices
                    if item.language == group[0] and item.language_name
                ),
                "",
            )
            facts.append(
                f"<b>{escape(tr('call.voice.group'))}</b> · "
                f"{escape(self._group_label(group, language_name=language_name))}"
            )
        if voice is not None:
            facts.extend(
                [
                    f"<b>{escape(tr('call.voice.id'))}</b> · <code>{escape(voice.id)}</code>",
                    f"<b>{escape(tr('call.voice.language'))}</b> · "
                    f"{escape(self._language_label(voice.language, voice.language_name))}",
                    f"<b>{escape(tr('call.voice.gender'))}</b> · "
                    f"{escape(self._gender_label(voice.gender))}",
                    f"<b>{escape(tr('call.voice.description'))}</b> · "
                    f"{escape(voice.description or tr('call.voice.description.unavailable'))}",
                ]
            )
        return facts

    def render(self, page="home"):
        ctx = self.config.context(self.owner, self.agent)
        tr = ui_language.tr
        rev = ctx["revision"][:12]
        profile, targets = ctx["profile"], ctx["targets"]

        def button(label, action, value=""):
            return InlineKeyboardButton(
                label, callback_data=f"call:{action}:{value}:{rev}"
            )

        def selected(kind):
            slot = profile[kind]
            return next(
                (t for t in targets if slot and t["id"] == slot["target_id"]), None
            )

        current = "/call" if ctx["call_ready"] else tr("call.off")
        facts = [
            f"<b>{escape(tr('call.' + kind))}</b> · {escape(str((selected(kind) or {}).get('label') or (selected(kind) or {}).get('model') or tr('call.off')))}"
            for kind in ("stt", "tts", "vision")
        ]
        rows = []
        title = tr("call.title")
        if page == "home":
            rows = [
                [
                    button(tr("call.stt"), "view", "stt"),
                    button(tr("call.tts"), "view", "tts"),
                ],
                [
                    button(tr("call.vision"), "view", "vision"),
                    button(tr("call.advanced"), "view", "advanced"),
                ],
            ]
            if not ctx["call_ready"]:
                rows = []
        elif page in ("stt", "tts", "vision"):
            title = tr("call." + page)
            choices = [t for t in targets if t["kind"] == page]
            for index, target in enumerate(choices):
                label = str(target.get("label") or target["model"])
                rows.append(
                    [
                        button(
                            selected_label(
                                label,
                                bool(
                                    profile[page]
                                    and profile[page]["target_id"] == target["id"]
                                ),
                            ),
                            "target",
                            f"{page}.{index}",
                        )
                    ]
                )
            if page == "vision":
                rows.append(
                    [
                        button(
                            selected_label(tr("call.off"), profile[page] is None),
                            "target",
                            "vision.off",
                        )
                    ]
                )
            if page == "tts":
                rows.append([button(tr("call.voice"), "view", "voice")])
        elif page == "voice" or page.startswith("voice."):
            title = tr("call.voice")
            target = selected("tts") or {}
            directory = self._directory_for(target)
            if directory is not None:
                try:
                    current_voice = directory.catalogue.voice(
                        profile["tts"]["voice_id"]
                    )
                except ComputeServiceError:
                    current_voice = None
                current = (
                    " · ".join(
                        part
                        for part in (
                            current_voice.label if current_voice else "",
                            profile["tts"]["voice_id"],
                        )
                        if part
                    )
                    if profile["tts"]
                    else tr("call.off")
                )
                groups = self._voice_groups(directory)
                if page == "voice":
                    facts = self._voice_facts(directory, current_voice)
                    for index, (key, voices) in enumerate(groups):
                        language_name = next(
                            (
                                voice.language_name
                                for voice in voices
                                if voice.language_name
                            ),
                            "",
                        )
                        rows.append(
                            [
                                button(
                                    self._group_label(
                                        key,
                                        len(voices),
                                        language_name,
                                    ),
                                    "view",
                                    f"voice.{index}",
                                )
                            ]
                        )
                else:
                    group_index = page.split(".", 1)[1]
                    key, voices = self.choice(groups, group_index)
                    language_name = next(
                        (
                            voice.language_name
                            for voice in voices
                            if voice.language_name
                        ),
                        "",
                    )
                    title = (
                        f"{title} · "
                        f"{self._group_label(key, language_name=language_name)}"
                    )
                    facts = self._voice_facts(directory, current_voice, key)
                    for voice in voices:
                        option_label = self._voice_option_label(voice)
                        rows.append(
                            [
                                button(
                                    selected_label(
                                        option_label,
                                        profile["tts"]["voice_id"] == voice.id,
                                    ),
                                    "cvoice",
                                    voice.id,
                                )
                            ]
                        )
            else:
                if page != "voice":
                    raise CallError("call_menu_invalid")
                for index, voice in enumerate(target.get("voices", [])):
                    label = voice_label(target, voice)
                    rows.append(
                        [
                            button(
                                selected_label(
                                    label,
                                    profile["tts"]["voice_id"] == voice,
                                ),
                                "voice",
                                str(index),
                            )
                        ]
                    )
                if self.voice_directory_error:
                    facts.append("⚠️ " + escape(tr("call.voice.catalogue.unavailable")))
            if profile["tts"]:
                if directory is None:
                    current = voice_label(target, profile["tts"]["voice_id"])
                rows.insert(0, [button(tr("call.voice.preview"), "preview")])
        elif page == "advanced":
            title = tr("call.advanced")
            for kind in ("stt", "tts", "vision"):
                if selected(kind) and selected(kind).get("options"):
                    rows.append([button(tr("call." + kind), "view", "options." + kind)])
            rows.append([button(tr("call.reset"), "reset")])
        elif page.startswith("options.") and page[8:] in ("stt", "tts", "vision"):
            kind = page[8:]
            target = selected(kind)
            facts = []
            for key_index, (key, spec) in enumerate(
                (target or {}).get("options", {}).items()
            ):
                value = profile[kind]["options"].get(key, tr("call.default"))
                facts.append(f"<b>{escape(key)}</b> · {escape(str(value))}")
                # Free values are entered through the normal slash command.
                choices = spec.get(
                    "enum", [True, False] if spec["type"] == "boolean" else []
                )
                for index, item in enumerate(choices):
                    rows.append(
                        [
                            button(
                                selected_label(f"{key}: {item}", value == item),
                                "option",
                                f"{kind}.{key_index}.{index}",
                            )
                        ]
                    )
                facts.append(f"<code>/call option {kind} {escape(key)} VALUE</code>")
        else:
            raise CallError("call_menu_invalid")
        rows.append([button(refresh_label(), "view", page)])
        if page != "home":
            if page.startswith("voice."):
                back_page = "voice"
            elif page == "voice":
                back_page = "tts"
            else:
                back_page = "home"
            rows.append([button(back_label(), "view", back_page)])
        voice_page = page == "voice" or page.startswith("voice.")
        voice_action = (
            "call.voice.catalogue.action"
            if voice_page and self._directory_for(selected("tts") or {}) is not None
            else "call.voice.action"
        )
        return setting_card(
            "☎️",
            title,
            current=escape(current),
            facts=facts,
            consequence=tr("call.next_call"),
            action=tr(voice_action if voice_page else "call.action"),
        ), InlineKeyboardMarkup(rows)

    def apply(self, action, value, revision):
        ctx = self.config.context(self.owner, self.agent)
        if revision != ctx["revision"][:12]:
            raise CallError("call_configuration_changed", 409)
        if action == "view":
            return value or "home"
        if action == "preview":
            return "voice"
        if action == "route":
            # Old buttons refresh the independent settings without changing either entrance.
            if value not in ("phone", "call"):
                raise CallError("call_menu_invalid")
            return "home"
        profile = copy.deepcopy(ctx["profile"])
        if action == "target":
            kind, index = value.split(".", 1)
            if kind not in ("stt", "tts", "vision"):
                raise CallError("call_menu_invalid")
            if kind == "vision" and index == "off":
                profile[kind] = None
            else:
                target = self.choice(
                    [t for t in ctx["targets"] if t["kind"] == kind], index
                )
                profile[kind] = {"target_id": target["id"], "options": {}}
                if kind == "tts":
                    profile[kind]["voice_id"] = target["voices"][0]
            page = kind
        elif action == "voice":
            target = next(
                t for t in ctx["targets"] if t["id"] == profile["tts"]["target_id"]
            )
            profile["tts"]["voice_id"] = self.choice(target["voices"], value)
            page = "voice"
        elif action == "cvoice":
            target = next(
                t for t in ctx["targets"] if t["id"] == profile["tts"]["target_id"]
            )
            directory = self._directory_for(target)
            if directory is None:
                raise CallError("call_voice_catalogue_unavailable", 503)
            try:
                voice = directory.catalogue.voice(value)
            except ComputeServiceError as exc:
                raise CallError("call_voice_unavailable", 409) from exc
            profile["tts"]["voice_id"] = voice.id
            page = self._voice_page(directory, voice.id)
            self.config.save_discovered_voice(
                self.owner,
                self.agent,
                ctx["revision"],
                profile,
                target_id=target["id"],
                voice_id=voice.id,
                voice_label=voice.label[:50],
            )
            return page
        elif action == "option":
            kind, key_index, index = value.split(".")
            target = next(
                t
                for t in ctx["targets"]
                if profile.get(kind) and t["id"] == profile[kind]["target_id"]
            )
            key = self.choice(list(target["options"]), key_index)
            spec = target["options"][key]
            choices = spec.get(
                "enum", [True, False] if spec["type"] == "boolean" else []
            )
            profile[kind]["options"][key] = self.choice(choices, index)
            page = "options." + kind
        elif action == "reset":
            doc, targets = self.config.read()
            profile = self.config.validate_profile(doc["default_profile"], targets)
            page = "home"
        else:
            raise CallError("call_menu_invalid")
        self.config.save(self.owner, self.agent, ctx["revision"], profile)
        return page

    async def preview(self, *, telegram=False, voice_id=None):
        doc, targets = self.config.read()
        slot = self.config.profile(self.owner, self.agent, doc, targets)["tts"]
        if not slot:
            return VoicePreviewDelivery("", (), "")
        target = targets[slot["target_id"]]
        voice = voice_id or slot["voice_id"]
        directory = self._directory_for(target)
        if directory is not None:
            try:
                directory.catalogue.voice(voice)
                downloaded = await directory.download_preview(
                    voice,
                    ui_language.current_locale(),
                )
            except ComputeServiceError:
                return VoicePreviewDelivery(
                    f"call:{target['model']}:{voice}",
                    (),
                    "",
                )
            display = self._voice_option_label(downloaded.voice)
            caption = ui_language.tr(
                "call.voice.catalogue.preview.caption",
                voice=display,
                language=self._language_label(downloaded.language),
            )
            return VoicePreviewDelivery(
                profile_id=f"call:{target['model']}:{voice}",
                assets=(),
                caption=caption,
                payload=downloaded.payload,
                media_type=downloaded.media_type,
                filename=downloaded.filename,
            )
        assets = get_call_preview(target, voice, telegram=telegram)
        return VoicePreviewDelivery(
            profile_id=f"call:{target['model']}:{voice}",
            assets=assets,
            caption=preview_caption(target, voice),
            media_type="audio/ogg" if telegram else "audio/mpeg",
            filename=assets[0][1].parent.name + assets[0][1].suffix if assets else "",
        )

    def command(self, args):
        action = args[0].casefold() if args else "menu"
        ctx = self.config.context(self.owner, self.agent)
        if action in ("menu", "status"):
            return "home"
        if action in ("activate", "deactivate") and len(args) == 1:
            return self.apply(
                "route",
                "call" if action == "activate" else "phone",
                ctx["revision"][:12],
            )
        if action in ("stt", "tts", "vision", "voice", "advanced") and len(args) == 1:
            return action
        if action == "reset" and len(args) == 1:
            return self.apply("reset", "", ctx["revision"][:12])
        if action == "option" and len(args) >= 4:
            kind, key = args[1:3]
            profile = copy.deepcopy(ctx["profile"])
            try:
                value = json.loads(" ".join(args[3:]))
            except ValueError:
                value = " ".join(args[3:])
            if kind not in profile or profile[kind] is None:
                raise CallError("call_option_unsupported")
            profile[kind]["options"][key] = value
            self.config.save(self.owner, self.agent, ctx["revision"], profile)
            return "options." + kind
        raise CallError("call_menu_invalid")


async def command(runtime, update, context):
    if not runtime._is_authorized_user(update.effective_user.id):
        return
    try:
        settings = CallSettings(runtime)
        page = settings.command(list(context.args or []))
        if page == "voice" or page.startswith("voice."):
            await settings.prepare_voice_directory()
        text, keyboard = settings.render(page)
        await runtime._reply_text(
            update, text, reply_markup=keyboard, parse_mode="HTML"
        )
    except (CallError, ValueError, IndexError, KeyError, StopIteration) as exc:
        await runtime._reply_text(
            update, ui_language.tr("call.error") + " · " + escape(str(exc))
        )


async def callback(runtime, update, context):
    query = update.callback_query
    if not runtime._is_authorized_user(query.from_user.id):
        return
    try:
        _, action, value, revision = query.data.split(":", 3)
        settings = CallSettings(runtime)
        if action in {"voice", "cvoice", "preview"} or (
            action == "view" and (value == "voice" or value.startswith("voice."))
        ):
            await settings.prepare_voice_directory()
        text, keyboard = settings.render(settings.apply(action, value, revision))
        telegram = (
            str(
                getattr(update, "_hashi_session_surface", None) or "telegram"
            ).casefold()
            == "telegram"
        )
        preview = (
            await settings.preview(telegram=telegram)
            if action in {"voice", "cvoice", "preview"}
            else None
        )
        if action != "preview":
            try:
                await query.edit_message_text(
                    text, reply_markup=keyboard, parse_mode="HTML"
                )
            except BadRequest as exc:
                if "message is not modified" not in str(exc).lower():
                    raise
        missing = preview is not None and not preview.available
        await query.answer(
            ui_language.tr("call.voice.preview.unavailable") if missing else None,
            show_alert=missing,
        )
        if preview and preview.available:
            assets = preview.assets
            if preview.payload is not None:
                with TemporaryDirectory(prefix="hashi-call-preview-") as temp_root:
                    filename = Path(preview.filename).name or "preview.wav"
                    path = Path(temp_root) / filename
                    path.write_bytes(preview.payload)
                    sent = await runtime._send_voice_profile_previews(
                        update,
                        preview.profile_id,
                        ((preview.renderer, path),),
                        caption_override=preview.caption,
                        media_type_override=preview.media_type,
                        filename_override=filename,
                        preview_id=str(getattr(query, "id", "")),
                    )
            else:
                sent = await runtime._send_voice_profile_previews(
                    update,
                    preview.profile_id,
                    assets,
                    caption_override=preview.caption,
                    media_type_override=preview.media_type,
                    filename_override=preview.filename,
                    preview_id=str(getattr(query, "id", "")),
                )
            if not sent:
                await runtime._reply_text(
                    update, ui_language.tr("call.voice.preview.unavailable")
                )
    except (CallError, ValueError, IndexError, KeyError, StopIteration) as exc:
        await query.answer(
            ui_language.tr("call.error") + " · " + str(exc), show_alert=True
        )
