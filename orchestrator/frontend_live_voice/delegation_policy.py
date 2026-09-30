"""Fail-safe PAO routing for GPT-Live client delegation proposals.

The live model owns the foreground conversation.  A client delegation is only
an execution request when the user's words clearly ask HASHI to inspect or
change external state.  Everything else stays in the foreground so ordinary
conversation cannot accidentally become a queue of Agent Runs.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re


class DelegationRoute(str, Enum):
    """Typed outcome for one provider delegation proposal."""

    DIRECT = "direct"
    CONFIRM = "confirm"
    EXECUTE = "execute"


@dataclass(frozen=True)
class DelegationDecision:
    route: DelegationRoute
    confidence: float
    reason: str


_NO_BACKEND_MARKERS = (
    "不用查",
    "不要查",
    "别查",
    "无需查",
    "不必查",
    "不用核对",
    "不要核对",
    "别核对",
    "不要验证",
    "不用验证",
    "知道什么就说什么",
    "直接说",
    "don't check",
    "do not check",
    "no need to check",
    "without checking",
    "stop checking",
    "don't verify",
    "do not verify",
)

_CONVERSATION_ONLY_MARKERS = (
    "快说",
    "赶紧说",
    "继续说",
    "详细说",
    "讲清楚",
    "回答",
    "解释",
    "为什么不说",
    "别兜圈子",
    "从昨天聊天到现在",
    "进到你的窗口",
    "hurry up",
    "answer me",
    "tell me why",
    "explain",
    "go on",
    "keep talking",
)

_EXECUTION_MARKERS = (
    "查一下",
    "查查",
    "查询",
    "核对一下",
    "核实",
    "验证一下",
    "检查一下",
    "搜索",
    "运行",
    "执行",
    "打开",
    "发送",
    "创建",
    "删除",
    "修复",
    "更新文件",
    "更新系统",
    "更新配置",
    "更新代码",
    "更新软件",
    "更新应用",
    "更新依赖",
    "升级",
    "安装",
    "重启",
    "停止任务",
    "继续处理",
    "构建",
    "部署",
    "帮我看看",
    "看一下",
    "look up",
    "check ",
    "search",
    "query",
    "verify",
    "inspect",
    "run ",
    "execute",
    "open ",
    "send ",
    "create ",
    "delete ",
    "fix ",
    "update the ",
    "update file",
    "update config",
    "update code",
    "update software",
    "update app",
    "update dependencies",
    "install ",
    "restart",
    "stop the ",
    "build ",
    "deploy",
)

_FRESHNESS_MARKERS = (
    "今天",
    "现在",
    "当前",
    "最新",
    "实时",
    "今日情况",
    "今日报告",
    "状态报告",
    "today",
    "right now",
    "current",
    "latest",
    "up-to-date",
    "real-time",
    "status report",
)

_AFFIRMATIVE_CONFIRMATIONS = frozenset(
    {
        "是",
        "对",
        "好",
        "好的",
        "可以",
        "行",
        "嗯",
        "查吧",
        "去查",
        "请查",
        "yes",
        "yes please",
        "please do",
        "go ahead",
        "check it",
    }
)


def _normalized(value: str) -> str:
    return re.sub(r"\s+", " ", str(value or "").strip().casefold())


def is_affirmative_confirmation(text: str) -> bool:
    """Return true only for a short answer to a pending backend-check offer."""

    normalized = _normalized(text)
    compact = re.sub(r"[\s,，。.!！?？]+", "", normalized)
    return normalized in _AFFIRMATIVE_CONFIRMATIONS or compact in {
        re.sub(r"[\s,，。.!！?？]+", "", item)
        for item in _AFFIRMATIVE_CONFIRMATIONS
    }


def route_delegation(text: str) -> DelegationDecision:
    """Classify a proposed background Run, defaulting to foreground speech."""

    normalized = _normalized(text)
    if not normalized:
        return DelegationDecision(
            DelegationRoute.DIRECT,
            1.0,
            "empty_or_unrecoverable_request",
        )
    if any(marker in normalized for marker in _NO_BACKEND_MARKERS):
        return DelegationDecision(
            DelegationRoute.DIRECT,
            1.0,
            "user_declined_backend_check",
        )
    if any(marker in normalized for marker in _EXECUTION_MARKERS):
        return DelegationDecision(
            DelegationRoute.EXECUTE,
            0.98,
            "explicit_backend_or_external_action",
        )
    if any(marker in normalized for marker in _CONVERSATION_ONLY_MARKERS):
        return DelegationDecision(
            DelegationRoute.DIRECT,
            0.99,
            "foreground_conversation_followup",
        )
    if any(marker in normalized for marker in _FRESHNESS_MARKERS):
        return DelegationDecision(
            DelegationRoute.CONFIRM,
            0.96,
            "freshness_may_help_but_execution_not_explicit",
        )
    return DelegationDecision(
        DelegationRoute.DIRECT,
        0.95,
        "no_explicit_backend_action",
    )


__all__ = [
    "DelegationDecision",
    "DelegationRoute",
    "is_affirmative_confirmation",
    "route_delegation",
]
