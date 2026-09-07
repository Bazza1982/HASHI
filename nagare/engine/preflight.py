"""
HASHI Flow — Pre-flight Collector
在工作流正式运行前，收集所有人工输入（一次性完成，不在执行中打扰用户）
"""

import json
from pathlib import Path
from typing import Optional


class PreFlightCollector:
    """
    交互式 pre-flight 信息收集器。
    读取 workflow YAML 中的 pre_flight.collect_from_human，
    向用户逐一提问并收集答案。
    支持 CLI 交互模式和静默模式（传入预填充值）。
    """

    def __init__(self, workflow: dict, prefill: Optional[dict] = None, silent: bool = False):
        """
        Args:
            workflow: 已解析的 workflow YAML 字典
            prefill: 预填充的答案字典（跳过已填充的问题）
            silent: 静默模式，所有问题使用默认值（用于测试）
        """
        self.workflow = workflow
        if prefill is not None and not isinstance(prefill, dict):
            raise ValueError("Pre-flight prefill must be a JSON object")
        self.prefill = prefill or {}
        self.silent = silent
        self.answers = {}

    def run(self) -> dict:
        """
        运行 pre-flight 收集流程。
        返回收集到的答案字典 {key: value}
        """
        pre_flight = self.workflow.get("pre_flight", {})
        questions = pre_flight.get("collect_from_human", [])
        defaults = pre_flight.get("defaults", {})
        if not isinstance(defaults, dict):
            raise ValueError("pre_flight.defaults must be a mapping")
        if not isinstance(questions, list):
            raise ValueError("pre_flight.collect_from_human must be a list")

        # Defaults provide non-interactive context; explicit prefill wins and may
        # include host-supplied keys that are not interactive questions.
        self.answers = dict(defaults)
        self.answers.update(self.prefill)

        if not questions:
            return dict(self.answers)

        wf_name = self.workflow.get("workflow", {}).get("name", "工作流")
        if not self.silent:
            self._print_header(wf_name, len(questions))

        for i, q in enumerate(questions, 1):
            if not isinstance(q, dict) or not isinstance(q.get("key"), str):
                raise ValueError("Each pre-flight question must be a mapping with a string key")
            key = q["key"]
            question_type = q.get("type", "text")
            if question_type not in {"text", "choice"}:
                raise ValueError(
                    f"Unsupported pre-flight question type for '{key}': {question_type}"
                )

            # 优先使用预填充值
            if key in self.prefill:
                value = self.prefill[key]
                self._validate_supplied_value(q, value)
                self.answers[key] = value
                if not self.silent:
                    self._print_prefilled(i, q, self.prefill[key])
                continue

            # 静默模式：使用默认值
            if self.silent:
                default = q.get("default", self.answers.get(key, ""))
                if q.get("required", False) and default in (None, ""):
                    raise ValueError(
                        f"Required pre-flight value '{key}' has no prefill or default"
                    )
                self._validate_supplied_value(q, default)
                self.answers[key] = default
                continue

            # 交互式提问
            answer = self._ask_question(i, q)
            self.answers[key] = answer

        if not self.silent:
            self._print_summary()
        return dict(self.answers)

    @staticmethod
    def _validate_supplied_value(question: dict, value) -> None:
        key = question["key"]
        if question.get("required", False) and value in (None, ""):
            raise ValueError(f"Required pre-flight value '{key}' is empty")
        if question.get("type", "text") == "choice" and value not in (None, ""):
            choices = question.get("choices", [])
            if value not in choices:
                raise ValueError(
                    f"Pre-flight value for '{key}' must be one of {choices!r}"
                )

    # =========================================================================
    # 提问逻辑
    # =========================================================================

    def _ask_question(self, idx: int, q: dict) -> str:
        """向用户提问，返回答案（字符串）"""
        question = q["question"]
        required = q.get("required", False)
        default = q.get("default", "")
        q_type = q.get("type", "text")
        choices = q.get("choices", [])

        print(f"\n[{idx}] {question}")

        if q_type == "choice" and choices:
            for j, c in enumerate(choices, 1):
                marker = " (默认)" if c == default else ""
                print(f"    {j}. {c}{marker}")
            prompt_str = f"    请选择 [1-{len(choices)}]"
            if default:
                prompt_str += "（直接回车选默认）"
            prompt_str += ": "

            while True:
                try:
                    raw = input(prompt_str).strip()
                    if not raw and default:
                        return default
                    idx_choice = int(raw) - 1
                    if 0 <= idx_choice < len(choices):
                        return choices[idx_choice]
                    print(f"    ⚠️  请输入 1-{len(choices)} 之间的数字")
                except ValueError:
                    print("    ⚠️  请输入数字")
                except EOFError:
                    return default or ""

        else:  # text
            if default:
                prompt_str = f"    输入（直接回车使用默认 \"{default}\"）: "
            elif required:
                prompt_str = "    输入（必填）: "
            else:
                prompt_str = "    输入（可选，直接回车跳过）: "

            while True:
                try:
                    raw = input(prompt_str).strip()
                    if not raw:
                        if default:
                            return default
                        elif required:
                            print("    ⚠️  此项为必填，请输入内容")
                            continue
                        else:
                            return ""
                    return raw
                except EOFError:
                    return default or ""

    # =========================================================================
    # 打印工具
    # =========================================================================

    def _print_header(self, wf_name: str, count: int):
        print(f"\n{'='*60}")
        print("  HASHI Flow — Pre-flight 信息收集")
        print(f"  工作流: {wf_name}")
        print(f"  需要回答 {count} 个问题（工作流运行前一次性收集）")
        print(f"{'='*60}")

    def _print_prefilled(self, idx: int, q: dict, value: str):
        print(f"\n[{idx}] {q['question']}")
        print(f"    ✅ 已预填充: {value}")

    def _print_summary(self):
        print(f"\n{'='*60}")
        print("  ✅ Pre-flight 完成，即将开始工作流...")
        print(f"{'='*60}\n")


def load_prefill_from_file(path: str) -> dict:
    """从 JSON 文件加载预填充答案（用于自动化/测试）"""
    p = Path(path)
    if not p.is_file():
        raise FileNotFoundError(f"Pre-flight prefill file does not exist: {p}")
    data = json.loads(p.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Pre-flight prefill file must contain a JSON object")
    return data
