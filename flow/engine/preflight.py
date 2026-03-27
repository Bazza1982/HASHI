"""
HASHI Flow — Pre-flight Collector
在工作流正式运行前，收集所有人工输入（一次性完成，不在执行中打扰用户）
"""

import json
import sys
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

        if not questions:
            return {}

        wf_name = self.workflow.get("workflow", {}).get("name", "工作流")
        self._print_header(wf_name, len(questions))

        for i, q in enumerate(questions, 1):
            key = q["key"]

            # 优先使用预填充值
            if key in self.prefill:
                self.answers[key] = self.prefill[key]
                self._print_prefilled(i, q, self.prefill[key])
                continue

            # 静默模式：使用默认值
            if self.silent:
                default = q.get("default", "")
                self.answers[key] = default
                continue

            # 交互式提问
            answer = self._ask_question(i, q)
            self.answers[key] = answer

        self._print_summary()
        return self.answers

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
                prompt_str += f"（直接回车选默认）"
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
                    print(f"    ⚠️  请输入数字")
                except EOFError:
                    return default or ""

        else:  # text
            if default:
                prompt_str = f"    输入（直接回车使用默认 \"{default}\"）: "
            elif required:
                prompt_str = f"    输入（必填）: "
            else:
                prompt_str = f"    输入（可选，直接回车跳过）: "

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
        print(f"  HASHI Flow — Pre-flight 信息收集")
        print(f"  工作流: {wf_name}")
        print(f"  需要回答 {count} 个问题（工作流运行前一次性收集）")
        print(f"{'='*60}")

    def _print_prefilled(self, idx: int, q: dict, value: str):
        print(f"\n[{idx}] {q['question']}")
        print(f"    ✅ 已预填充: {value}")

    def _print_summary(self):
        print(f"\n{'='*60}")
        print(f"  ✅ Pre-flight 完成，即将开始工作流...")
        print(f"{'='*60}\n")


def load_prefill_from_file(path: str) -> dict:
    """从 JSON 文件加载预填充答案（用于自动化/测试）"""
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}
