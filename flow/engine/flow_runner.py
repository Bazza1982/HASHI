"""
HASHI compatibility wrapper around the extracted Nagare core runner.
"""

from __future__ import annotations

import json
from pathlib import Path

from nagare.engine.runner import FlowRunner as CoreFlowRunner

from flow.adapters.hashi import (
    HChatNotifier,
    HASHIEvaluator,
    HASHIStepHandler,
    ensure_hashi_evaluator,
    ensure_hashi_notifier,
    ensure_hashi_step_handler,
)


ROOT = Path(__file__).resolve().parents[2]


class FlowRunner(CoreFlowRunner):
    def __init__(self, workflow_path: str, run_id: str | None = None, **kwargs):
        repo_root = kwargs.pop("repo_root", ROOT)
        runs_root = kwargs.pop("runs_root", ROOT / "flow" / "runs")
        notifier = ensure_hashi_notifier(kwargs.pop("notifier", None))
        evaluator = ensure_hashi_evaluator(kwargs.pop("evaluator", None))
        step_handler = ensure_hashi_step_handler(
            kwargs.pop("step_handler", None),
            run_id=run_id,
            repo_root=repo_root,
            runs_root=runs_root,
        )
        super().__init__(
            workflow_path,
            run_id=run_id,
            step_handler=step_handler,
            notifier=notifier,
            evaluator=evaluator,
            repo_root=repo_root,
            runs_root=runs_root,
            **kwargs,
        )
        self._bind_hashi_adapters()

    def _bind_hashi_adapters(self) -> None:
        workflow_id = self.workflow.get("workflow", {}).get("id")
        for adapter in (self.step_handler, self.notifier, self.evaluator):
            bind = getattr(adapter, "bind_runtime_context", None)
            if callable(bind):
                bind(
                    run_id=self.run_id,
                    workflow_id=workflow_id,
                    trace_id=self.trace_id,
                    event_logger=self.event_logger,
                )


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("用法: python flow_runner.py <workflow.yaml>")
        raise SystemExit(1)

    runner = FlowRunner(sys.argv[1])
    result = runner.start()
    print(json.dumps(result, ensure_ascii=False, indent=2))
