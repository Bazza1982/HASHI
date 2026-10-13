from types import SimpleNamespace

from orchestrator.frontend_call.ports import HashiPorts


def test_call_activity_port_exposes_answer_and_public_commentary_only():
    observed = {}

    class Activity:
        def poll(self, request_id, *, after_sequence, limit):
            observed.update(
                request_id=request_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return {
                "ok": True,
                # More events may exist beyond this bounded page. The returned
                # cursor must stop at the last observed event instead of
                # skipping directly to the store's latest sequence.
                "latest_sequence": 99,
                "events": [
                    {
                        "sequence": 5,
                        "event_id": "reasoning-5",
                        "delivery_class": "reasoning",
                        "presentation_channel": "thinking",
                        "summary": "private reasoning",
                    },
                    {
                        "sequence": 6,
                        "event_id": "commentary-6",
                        "delivery_class": "user_commentary",
                        "presentation_channel": "commentary",
                        "summary": "I am checking that now.",
                    },
                    {
                        "sequence": 7,
                        "event_id": "answer-7",
                        "delivery_class": "answer_preview",
                        "presentation_channel": "answer",
                        "summary": "The first sentence is ready.",
                    },
                    {
                        "sequence": 8,
                        "event_id": "technical-8",
                        "delivery_class": "technical",
                        "presentation_channel": "verbose",
                        "summary": "internal transport detail",
                    },
                ],
            }

    class Store:
        @staticmethod
        def get_run(run_id, *, owner_id):
            assert run_id == "run-1"
            assert owner_id == "owner"
            return {
                "session_id": "session-1",
                "context_generation": 2,
                "request_id": "request-1",
                "state": "running",
            }

    runtime = SimpleNamespace(request_activity=Activity())
    api = SimpleNamespace(
        session_store=Store(),
        _runtime_map=lambda: {"agent-1": runtime},
    )
    binding = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "context_generation": 2,
    }

    result = HashiPorts(api).result("owner", binding, "run-1", after_sequence=4)

    assert observed == {
        "request_id": "request-1",
        "after_sequence": 4,
        "limit": 256,
    }
    assert result["activity_sequence"] == 8
    assert [
        (event["delivery_class"], event["presentation_channel"])
        for event in result["events"]
    ] == [
        ("user_commentary", "commentary"),
        ("answer_preview", "answer"),
    ]


async def test_call_activity_port_reads_the_isolated_worker_instead_of_stale_local_store():
    observed = {}

    class Store:
        @staticmethod
        def get_run(run_id, *, owner_id):
            assert run_id == "run-1"
            assert owner_id == "owner"
            return {
                "session_id": "session-1",
                "context_generation": 2,
                "request_id": "request-1",
                "state": "running",
            }

    class StaleActivity:
        @staticmethod
        def poll(*_args, **_kwargs):
            observed["stale_local_store_used"] = True
            return {"ok": True, "latest_sequence": 0, "events": []}

    class Runtime:
        request_activity = StaleActivity()

        @staticmethod
        async def poll_request_activity(request_id, *, after_sequence, limit):
            observed.update(
                request_id=request_id,
                after_sequence=after_sequence,
                limit=limit,
            )
            return {
                "ok": True,
                "latest_sequence": 6,
                "events": [
                    {
                        "sequence": 6,
                        "event_id": "commentary-6",
                        "delivery_class": "user_commentary",
                        "presentation_channel": "commentary",
                        "summary": "I am checking that now.",
                    }
                ],
            }

    api = SimpleNamespace(
        session_store=Store(),
        _runtime_map=lambda: {"agent-1": Runtime()},
    )
    binding = {
        "agent_id": "agent-1",
        "session_id": "session-1",
        "context_generation": 2,
    }
    projected = HashiPorts(api).result("owner", binding, "run-1", after_sequence=4)
    if hasattr(projected, "__await__"):
        projected = await projected

    assert observed == {
        "request_id": "request-1",
        "after_sequence": 4,
        "limit": 256,
    }
    assert projected["activity_sequence"] == 6
    assert projected["events"][0]["delivery_class"] == "user_commentary"
