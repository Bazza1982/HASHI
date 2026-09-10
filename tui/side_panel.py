"""Read-only information panel for the built-in HASHI TUI."""

from __future__ import annotations

from rich.text import Text
from textual.widgets import Static


LABELS = {
    "zh": {
        "title": "信息面板",
        "readonly": "只读 · 操作请使用输入框中的 /commands",
        "offline": "API 离线",
        "refreshing": "正在刷新…",
        "unavailable": "暂不可用",
        "partial": "部分信息暂不可用",
        "token": "Token 用量",
        "session": "本次会话",
        "all_time": "全部时间",
        "requests": "请求",
        "input": "输入",
        "output": "输出",
        "thinking": "推理",
        "cost_unknown": "次费用未知",
        "cost_unknown_all": "费用未知",
        "jobs": "任务",
        "scheduled": "定时",
        "background": "后台",
        "none": "无",
        "enabled": "启用",
        "disabled": "停用",
        "context": "HASHI 上下文",
        "prompts": "系统提示",
        "active": "生效",
        "configured": "已配置",
        "characters": "字符",
        "parked": "暂存主题",
        "agents": "代理",
        "online": "在线",
        "offline_agent": "离线",
        "busy": "忙碌",
        "queue": "队列",
    },
    "en": {
        "title": "Information",
        "readonly": "Read only · use /commands in the input to act",
        "offline": "API offline",
        "refreshing": "Refreshing…",
        "unavailable": "Unavailable",
        "partial": "Some information is unavailable",
        "token": "Token usage",
        "session": "Session",
        "all_time": "All time",
        "requests": "requests",
        "input": "input",
        "output": "output",
        "thinking": "thinking",
        "cost_unknown": "unknown-cost requests",
        "cost_unknown_all": "cost unknown",
        "jobs": "Jobs",
        "scheduled": "Scheduled",
        "background": "Background",
        "none": "None",
        "enabled": "enabled",
        "disabled": "disabled",
        "context": "HASHI context",
        "prompts": "System prompts",
        "active": "active",
        "configured": "configured",
        "characters": "chars",
        "parked": "Parked topics",
        "agents": "Agents",
        "online": "online",
        "offline_agent": "offline",
        "busy": "busy",
        "queue": "queue",
    },
}


class SidePanel(Static):
    """Scrollable projection of canonical HASHI state, with no actions."""

    can_focus = False
    DEFAULT_CSS = """
    SidePanel {
        display: none;
        width: 42;
        min-width: 24;
        max-width: 45%;
        height: 1fr;
        margin-left: 1;
        padding: 0 1;
        background: #08131d;
        color: #dff6ff;
        border: solid #2a5b82;
        border-title-align: left;
        overflow-y: auto;
        scrollbar-background: #050b12;
        scrollbar-color: #2a5b82;
        scrollbar-color-hover: #71b7ff;
        scrollbar-color-active: #63ffd9;
    }
    """

    def __init__(self, *args, **kwargs):
        super().__init__("", *args, **kwargs)
        self._content = Text("")

    @staticmethod
    def _number(value) -> str:
        try:
            return f"{int(value or 0):,}"
        except (TypeError, ValueError):
            return "0"

    @staticmethod
    def _cost(value) -> str:
        try:
            return f"${float(value or 0):,.4f}"
        except (TypeError, ValueError):
            return "$0.0000"

    @staticmethod
    def _line(rows: Text, value: str, style: str = "#dff6ff") -> None:
        rows.append(value, style=style)
        rows.append("\n")

    @classmethod
    def _section(cls, rows: Text, value: str) -> None:
        if rows:
            rows.append("\n")
        cls._line(rows, value, "bold #63ffd9")

    @classmethod
    def _usage(cls, rows: Text, overview: dict | None, labels: dict) -> None:
        cls._section(rows, labels["token"])
        usage = overview.get("usage") if isinstance(overview, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        for key, title in (
            ("session", labels["session"]),
            ("all_time", labels["all_time"]),
        ):
            period = usage.get(key)
            if not isinstance(period, dict):
                cls._line(rows, f"{title} · {labels['unavailable']}", "dim #9fb3c8")
                continue
            requests = int(period.get("requests", 0) or 0)
            unknown = int(period.get("unknown_cost_requests", 0) or 0)
            cls._line(
                rows,
                f"{title} · {cls._number(period.get('total'))} · "
                f"{cls._number(requests)} {labels['requests']}",
            )
            cls._line(
                rows,
                f"  {labels['input']} {cls._number(period.get('input'))} · "
                f"{labels['output']} {cls._number(period.get('output'))} · "
                f"{labels['thinking']} {cls._number(period.get('thinking'))}",
                "dim #9be7ff",
            )
            if unknown and (not requests or unknown >= requests):
                cost = labels["cost_unknown_all"]
            else:
                cost = cls._cost(period.get("cost_usd"))
                if unknown:
                    cost += f" · {unknown} {labels['cost_unknown']}"
            cls._line(rows, f"  {cost}", "dim #9be7ff")

    @classmethod
    def _jobs(
        cls,
        rows: Text,
        jobs: list[dict] | None,
        title: str,
        labels: dict,
        *,
        scheduled: bool,
    ) -> None:
        if jobs is None:
            cls._line(rows, f"{title} · {labels['unavailable']}", "dim #9fb3c8")
            return
        cls._line(rows, f"{title} · {len(jobs)}")
        if not jobs:
            cls._line(rows, f"  {labels['none']}", "dim #9fb3c8")
        for item in jobs[:8]:
            if not isinstance(item, dict):
                continue
            identifier = str(item.get("job_id") or item.get("id") or "?")
            if scheduled:
                last_run = (
                    item.get("last_run")
                    if isinstance(item.get("last_run"), dict)
                    else {}
                )
                state = str(
                    last_run.get("status")
                    or last_run.get("state")
                    or item.get("last_status")
                    or ""
                )
                enabled = (
                    labels["enabled"]
                    if item.get("enabled", False)
                    else labels["disabled"]
                )
                detail = f"{item.get('kind') or 'job'} · {enabled}" + (
                    f" · {state}" if state else ""
                )
            else:
                detail = str(item.get("state") or item.get("status") or "—")
            cls._line(rows, f"  {identifier} · {detail}")
        if len(jobs) > 8:
            cls._line(rows, f"  … +{len(jobs) - 8}", "dim #9fb3c8")

    @classmethod
    def _context_section(cls, rows: Text, overview: dict | None, labels: dict) -> None:
        cls._section(rows, labels["context"])
        prompts = overview.get("system_prompts") if isinstance(overview, dict) else None
        if not isinstance(prompts, dict):
            cls._line(
                rows, f"{labels['prompts']} · {labels['unavailable']}", "dim #9fb3c8"
            )
            return
        cls._line(
            rows,
            f"{labels['prompts']} · {cls._number(prompts.get('active_count'))} {labels['active']} · "
            f"{cls._number(prompts.get('configured_count'))}/{cls._number(prompts.get('total_count'))} "
            f"{labels['configured']}",
        )
        slots = prompts.get("slots") if isinstance(prompts.get("slots"), list) else []
        configured = [
            item
            for item in slots
            if isinstance(item, dict) and item.get("state") != "empty"
        ]
        for item in configured[:8]:
            cls._line(
                rows,
                f"  {item.get('slot') or '?'} · {str(item.get('state') or 'off').upper()} · "
                f"{cls._number(item.get('characters'))} {labels['characters']}",
            )
            preview = str(item.get("preview") or "").strip()
            if preview:
                cls._line(rows, f"    {preview}", "dim #9fb3c8")
        if len(configured) > 8:
            cls._line(rows, f"  … +{len(configured) - 8}", "dim #9fb3c8")

    @classmethod
    def _parked(cls, rows: Text, overview: dict | None, labels: dict) -> None:
        cls._section(rows, labels["parked"])
        parked = overview.get("parked_topics") if isinstance(overview, dict) else None
        if not isinstance(parked, dict):
            cls._line(rows, labels["unavailable"], "dim #9fb3c8")
            return
        topics = parked.get("topics") if isinstance(parked.get("topics"), list) else []
        if not topics:
            cls._line(rows, labels["none"], "dim #9fb3c8")
        for item in topics[:8]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or item.get("summary_short") or "—")
            followup = (
                item.get("followup") if isinstance(item.get("followup"), dict) else {}
            )
            state = str(followup.get("status") or "").strip()
            cls._line(
                rows,
                f"  #{item.get('slot', '?')} · {title}"
                + (f" · {state}" if state else ""),
            )
        if len(topics) > 8:
            cls._line(rows, f"  … +{len(topics) - 8}", "dim #9fb3c8")

    @classmethod
    def _agents(
        cls, rows: Text, agents: list[dict], selected: str | None, labels: dict
    ) -> None:
        cls._section(rows, labels["agents"])
        if not agents:
            cls._line(rows, labels["none"], "dim #9fb3c8")
        for item in agents:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("id") or "?")
            display = str(item.get("display_name") or name)
            emoji = str(item.get("emoji") or "").strip()
            queue_depth = int(item.get("queue_depth", 0) or 0)
            online = bool(item.get("online"))
            busy = bool(item.get("is_generating")) or queue_depth > 0
            state = (
                labels["busy"]
                if busy
                else labels["online"]
                if online
                else labels["offline_agent"]
            )
            engine = str(item.get("active_backend") or item.get("engine") or "").strip()
            value = f"{'›' if name == selected else ' '} {'●' if online else '○'} "
            value += f"{emoji + ' ' if emoji else ''}{display} [{name}] · {state}"
            if engine:
                value += f" · {engine}"
            if queue_depth:
                value += f" · {labels['queue']} {queue_depth}"
            cls._line(rows, value, "#c7ff8a" if name == selected else "#dff6ff")

    def update_dashboard(
        self,
        *,
        instance_id: str,
        current_agent: str | None,
        current_agent_display: str,
        current_backend: str,
        gateway_ok: bool,
        overview: dict | None,
        scheduler_jobs: list[dict] | None,
        background_jobs: list[dict] | None,
        agents: list[dict],
        language: str,
        loading: bool = False,
        incomplete: bool = False,
    ) -> None:
        labels = LABELS["zh" if language == "zh" else "en"]
        self.border_title = labels["title"]
        rows = Text()
        self._line(rows, instance_id or "HASHI", "bold #9be7ff")
        agent_label = current_agent_display or current_agent or "—"
        if (
            current_agent
            and current_agent_display
            and current_agent != current_agent_display
        ):
            agent_label = f"{current_agent_display} [{current_agent}]"
        self._line(
            rows,
            agent_label + (f" · {current_backend}" if current_backend else ""),
            "bold #dff6ff",
        )
        self._line(rows, labels["readonly"], "dim #9be7ff")
        if not gateway_ok:
            self._line(rows, labels["offline"], "#ff7a7a")
        elif loading:
            self._line(rows, labels["refreshing"], "dim #9be7ff")
        elif incomplete:
            self._line(rows, labels["partial"], "#ffd479")

        self._usage(rows, overview, labels)
        self._section(rows, labels["jobs"])
        self._jobs(rows, scheduler_jobs, labels["scheduled"], labels, scheduled=True)
        self._jobs(rows, background_jobs, labels["background"], labels, scheduled=False)
        self._context_section(rows, overview, labels)
        self._parked(rows, overview, labels)
        self._agents(rows, agents, current_agent, labels)
        self._content = rows
        self.update(rows)

    def render(self) -> Text:
        return self._content
