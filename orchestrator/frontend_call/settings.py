"""Backend-owned /call menus using the existing command interaction contract."""

import copy
import json
from html import escape
from pathlib import Path
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest
from orchestrator import runtime_session, ui_language
from orchestrator.command_ui import (
    setting_card,
    selected_label,
    back_label,
    refresh_label,
)
from orchestrator.tts_voice_previews import get_tts_voice_preview_assets
from .config import CallConfig
from .contract import CallError
from .voice_catalog import voice_label
from .voice_previews import get_call_preview, preview_caption


class CallSettings:
    VOICE_PAGE_SIZE = 12

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
        project_root = Path(
            getattr(runtime.global_config, "project_root", None) or root
        )
        catalog_default = project_root / "orchestrator" / "voice_catalog.default.json"
        catalog_options = (
            {
                "voice_catalog_path": project_root / "voice_catalog.json",
                "project_root": project_root,
            }
            if catalog_default.is_file()
            else {}
        )
        self.config = CallConfig(
            Path(root) / "call_profiles.json",
            **catalog_options,
        )
        self.config.initialize()
        self.owner, self.agent = runtime_session.owner_id(runtime), runtime.name

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
            try:
                requested_page = int(page.partition(".")[2] or "0")
            except ValueError as exc:
                raise CallError("call_menu_invalid") from exc
            voices = list(target.get("voices", []))
            total_pages = max(
                1, (len(voices) + self.VOICE_PAGE_SIZE - 1) // self.VOICE_PAGE_SIZE
            )
            voice_page = min(max(0, requested_page), total_pages - 1)
            start = voice_page * self.VOICE_PAGE_SIZE
            for index, voice in enumerate(
                voices[start : start + self.VOICE_PAGE_SIZE], start=start
            ):
                label = voice_label(target, voice)
                rows.append(
                    [
                        button(
                            selected_label(label, profile["tts"]["voice_id"] == voice),
                            "voice",
                            str(index),
                        )
                    ]
                )
            if profile["tts"]:
                current = voice_label(target, profile["tts"]["voice_id"])
                rows.insert(0, [button(tr("call.voice.preview"), "preview")])
            navigation = []
            if voice_page:
                navigation.append(
                    button(tr("voice.tts.previous"), "view", f"voice.{voice_page - 1}")
                )
            if voice_page + 1 < total_pages:
                navigation.append(
                    button(tr("voice.tts.next"), "view", f"voice.{voice_page + 1}")
                )
            if navigation:
                rows.append(navigation)
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
            rows.append(
                [
                    button(
                        back_label(),
                        "view",
                        "tts"
                        if page == "voice" or page.startswith("voice.")
                        else "home",
                    )
                ]
            )
        return setting_card(
            "☎️",
            title,
            current=escape(current),
            facts=facts,
            consequence=tr("call.next_call"),
            action=tr("call.voice.action" if page == "voice" else "call.action"),
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
            page_number = int(value) // self.VOICE_PAGE_SIZE
            page = "voice" if page_number == 0 else f"voice.{page_number}"
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

    def preview(self, *, telegram=False):
        doc, targets = self.config.read()
        slot = self.config.profile(self.owner, self.agent, doc, targets)["tts"]
        if not slot:
            return "", (), ""
        target = targets[slot["target_id"]]
        voice = slot["voice_id"]
        metadata = (target.get("voice_metadata") or {}).get(voice)
        if isinstance(metadata, dict):
            preset = {**metadata, "provider": target["id"]}
            assets = get_tts_voice_preview_assets(
                voice,
                preset,
                telegram=telegram,
            )
            if not assets and preset.get("voice_name"):
                assets = get_call_preview(
                    target,
                    preset["voice_name"],
                    telegram=telegram,
                )
            return (
                f"call:{target['id']}:{voice}",
                assets,
                preview_caption(target, voice),
            )
        return (
            f"call:{target['model']}:{voice}",
            get_call_preview(target, voice, telegram=telegram),
            preview_caption(target, voice),
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
        text, keyboard = settings.render(settings.command(list(context.args or [])))
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
        text, keyboard = settings.render(settings.apply(action, value, revision))
        telegram = (
            str(
                getattr(update, "_hashi_session_surface", None) or "telegram"
            ).casefold()
            == "telegram"
        )
        preview = (
            settings.preview(telegram=telegram)
            if action in {"voice", "preview"}
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
        missing = preview is not None and not preview[1]
        await query.answer(
            ui_language.tr("call.voice.preview.unavailable") if missing else None,
            show_alert=missing,
        )
        if preview and preview[1]:
            profile_id, assets, caption = preview
            sent = await runtime._send_voice_profile_previews(
                update,
                profile_id,
                assets,
                caption_override=caption,
                media_type_override="audio/ogg" if telegram else "audio/mpeg",
                filename_override=assets[0][1].parent.name + assets[0][1].suffix,
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
