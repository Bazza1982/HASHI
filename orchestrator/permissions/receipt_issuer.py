"""Private supervisor receipt issuer; it is never an agent/FC RPC method.

The trusted supervisor must establish the observation before signing. This
utility authenticates its attestation, not the physical observation itself.
Human approval keys are separate and are never accepted here as that authority.
"""
from __future__ import annotations

import base64
from dataclasses import asdict
from hashlib import sha256
import json
import re
import time
from uuid import uuid4

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from orchestrator.permissions.authority import OperationOutcomeReceipt, ResourceStopReceipt
from orchestrator.permissions.models import ExecutionContext

_STOP = b"hashi-permission-stop-v1\0"
_OUTCOME = b"hashi-permission-outcome-v1\0"


class SupervisorReceiptIssuer:
    def __init__(self, *, key_id, signing_key, clock=time.time):
        if (not isinstance(key_id, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", key_id) is None
                or not isinstance(signing_key, Ed25519PrivateKey) or not callable(clock)):
            raise ValueError("a protected supervisor identity and signing key are required")
        self.key_id, self._key, self._clock = key_id, signing_key, clock

    @property
    def public_key(self):
        return self._key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def _context(self, context):
        if not isinstance(context, ExecutionContext) or context.executor_id != self.key_id:
            raise ValueError("the receipt observer differs from this supervisor")

    def _reference(self, receipt, *, domain, prefix):
        data = json.dumps(asdict(receipt), sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        signature = self._key.sign(domain + self.key_id.encode("ascii") + b"\0" + data)
        def b64(value):
            return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")

        return ".".join((prefix, self.key_id, b64(data), b64(signature)))

    def issue_stop(self, context, claim_id, observation):
        self._context(context)
        if not isinstance(claim_id, str) or not claim_id or not isinstance(observation, dict) or not observation:
            raise ValueError("an exact claim and supervisor stop observation are required")
        encoded = json.dumps(observation, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if len(encoded) > 65536:
            raise ValueError("supervisor observation exceeds the receipt limit")
        receipt = ResourceStopReceipt(
            nonce=uuid4().hex, claim_id=claim_id, instance_id=context.instance_id,
            task_id=context.task_id, execution_epoch=context.execution_epoch, observer_id=self.key_id,
            evidence_digest=sha256(encoded).hexdigest(), expires_at=float(self._clock()) + 30,
        )
        return self._reference(receipt, domain=_STOP, prefix="hps1")

    def issue_outcome(self, context, claim_id, result):
        self._context(context)
        if (result.outcome not in {"succeeded", "failed"} or not isinstance(result.payload, str)
                or sha256(result.payload.encode("utf-8")).hexdigest() != result.result_digest):
            raise ValueError("a complete persisted operation outcome is required")
        receipt = OperationOutcomeReceipt(
            nonce=uuid4().hex, claim_id=claim_id, operation_id=result.operation_id,
            arguments_digest=result.arguments_digest, instance_id=context.instance_id,
            task_id=context.task_id, execution_epoch=context.execution_epoch, observer_id=self.key_id,
            outcome=result.outcome, result_digest=result.result_digest, expires_at=float(self._clock()) + 30,
        )
        return self._reference(receipt, domain=_OUTCOME, prefix="hpo1")
