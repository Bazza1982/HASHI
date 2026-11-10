"""Trusted HTML presentation only; no identity, policy or decision writer."""
from __future__ import annotations

import base64
from contextvars import ContextVar
import hashlib
from html import escape
from pathlib import Path
import re

from orchestrator.ui_language import tr
from orchestrator.permissions.presentation import request_summary

THEME = ContextVar("approval_page_theme", default="bright")
MODE = ContextVar("approval_page_mode", default="chat")
TOKENS = Path(__file__).with_name("approval_theme_tokens.css").read_text(encoding="utf-8")
THEMES = frozenset(re.findall(r"\.app\.theme-([a-z0-9-]+)", TOKENS))
SIMPLE_THEMES = frozenset(re.findall(r"\.simple-theme-([a-z0-9-]+)", TOKENS))
CSS = TOKENS + """
.simple-mode{--ui-on-brand:var(--simple-send-text);min-height:100dvh;display:flow-root;background:var(--ui-surface-app);color:var(--ui-text-primary)}
*{box-sizing:border-box}body{margin:0;min-height:100dvh;background:var(--ui-surface-app);color:var(--ui-text-primary);font:15px/1.55 var(--ui-font-ui)}
main{font-family:var(--ui-font-ui);max-width:600px;margin:6vh auto;padding:var(--ui-space-6);background:var(--ui-surface-card);border:1px solid var(--ui-border-normal);border-radius:var(--ui-radius-panel);box-shadow:var(--ui-shadow-3)}
h1{font-size:22px;margin:0 0 24px}h2{font-size:16px;margin:0}p{margin:12px 0;overflow-wrap:anywhere}
label{display:grid;gap:6px;color:var(--ui-text-secondary)}input:not([type=hidden]){width:100%;min-height:44px;padding:10px 12px;border:1px solid var(--ui-border-normal);border-radius:var(--ui-radius-control);background:var(--ui-surface-control);color:var(--ui-text-primary);font:inherit}
button,summary{cursor:pointer}button{min-height:44px;padding:10px 16px;font:inherit;border:1px solid var(--ui-border-normal);border-radius:var(--ui-radius-control);background:var(--ui-surface-control);color:var(--ui-text-primary)}
button:focus-visible,input:focus-visible,summary:focus-visible{outline:2px solid var(--ui-focus-ring);outline-offset:3px}
form:has(input[name=operation][value=login]) button{width:100%}
.primary button,form:has(input[name=operation][value=login]) button{background:var(--ui-brand-primary);color:var(--ui-on-brand);border-color:var(--ui-brand-primary)}
.actions{display:flex;flex-wrap:wrap;gap:12px;padding:16px 0}.actions>*{flex:1}.actions form{flex:1}.actions button{width:100%}
.more{border-top:1px solid var(--ui-border-normal);padding:12px 0}.more form{margin-top:8px}.more button{width:100%;text-align:start}
.resources{list-style:none;padding:0;display:grid;gap:16px}.resources li{padding:16px;border:1px solid var(--ui-border-normal);border-radius:var(--ui-radius-control)}
.resources strong,.resources span,code,time{display:block}code{white-space:pre-wrap;overflow-wrap:anywhere;user-select:text;margin:8px 0}
details{margin:12px 0}summary{min-height:44px;align-content:center;color:var(--ui-text-secondary)}
.warning{padding:12px;border-inline-start:3px solid var(--ui-status-warning);background:var(--ui-surface-warning)}
.note,.eyebrow{color:var(--ui-text-secondary)}.eyebrow{font-size:13px;margin-top:0}dt{color:var(--ui-text-secondary)}dd{margin:0 0 8px;overflow-wrap:anywhere}
[role=alert]{padding:12px;border:1px solid var(--ui-status-warning)}[role=status]{font-weight:600}
@media(max-width:640px){main{margin:0;max-width:none;min-height:100dvh;padding:24px 16px;border-radius:0}.actions{position:sticky;bottom:0;background:var(--ui-surface-card);padding-bottom:max(16px,env(safe-area-inset-bottom))}}
"""
STYLE_HASH = base64.b64encode(hashlib.sha256(CSS.encode()).digest()).decode()


def page_theme(value):
    allowed = SIMPLE_THEMES if MODE.get() == "simple" else THEMES
    return value if value in allowed else "bright"


def review_details(view, subject, locale):
    def label(key):
        return escape(tr("safety." + key, locale=locale))

    summary = request_summary(view)
    body = '<p class="eyebrow">' + label("human_agent") + " · " + escape(subject.agent_id) + "</p>"
    body += "<h2>" + label("resources") + "</h2>"
    if summary["purpose"]:
        body += "<p>" + escape(summary["purpose"]) + "</p>"
    body += '<ul class="resources">'
    for scope in summary["resources"]:
        body += "<li><strong>" + escape(scope["name"]) + "</strong><span>"
        body += " · ".join(escape(tr("safety.action." + action, locale=locale)) for action in scope["actions"])
        body += "</span><details><summary>" + label("human_location") + "</summary><code>"
        body += escape(scope["resource"]) + "</code></details></li>"
    body += "</ul>"
    if any("write" in scope["actions"] for scope in summary["resources"]):
        body += '<p class="warning">' + label("human_write_warning") + "</p>"
    body += "<details><summary>" + label("human_details") + "</summary><dl>"
    for key, value in (("task_id", view.request.task_id), ("request_id", view.request.request_id)):
        body += "<dt>" + label(key) + "</dt><dd>" + escape(value) + "</dd>"
    body += "</dl></details>"
    return body
# HASHI-Fingerprint: hfp_971f4b45890242c18e44351acf112608
