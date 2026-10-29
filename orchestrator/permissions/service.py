"""Single authority host and closed trusted PAO/FC service surfaces.

The application is inert until an operator-owned service calls start(). It
never starts HASHI, a Worker, another account or an executor during import.
"""
from __future__ import annotations

import inspect
from uuid import uuid4

from orchestrator.permissions.authority import PermissionDenied
from orchestrator.permissions.clients import PAO_METHODS, CONTROL_METHODS
from orchestrator.permissions.control_plane import PermissionControlPlane
from orchestrator.permissions.run_service import PermissionRunService, binding_from_route
from orchestrator.permissions.wire import PermissionServiceRouter


class PermissionServiceApplication:
    def __init__(self, authority, run_store, identity_service, *, policy_resolver,
                 executor_factory, key_id, signing_key, owner_bindings, levels,
                 settings=None, management=None):
        if not isinstance(owner_bindings, dict) or not owner_bindings:
            raise ValueError("explicit human user to PAO owner bindings are required")
        if not isinstance(levels, tuple) or not levels or set(levels) - {"L1", "L2"}:
            raise ValueError("qualified installed service levels are required")
        self.authority, self.store = authority, run_store
        self.identity, self.owners = identity_service, dict(owner_bindings)
        self.generation = uuid4().hex
        self.levels, self._factory = levels, executor_factory
        self.runs = PermissionRunService(
            authority, run_store, policy_resolver=policy_resolver, executor_factory=executor_factory,
        )
        self.control = PermissionControlPlane(
            authority, run_store, identity_service, key_id=key_id, signing_key=signing_key,
            owner_bindings=self.owners, settings=settings,
        )
        self._management, self._endpoints, self._started = management, [], False
        self.pao_router = PermissionServiceRouter(self, methods=PAO_METHODS)
        self.control_router = PermissionServiceRouter(self, methods=CONTROL_METHODS)

    async def start(self, endpoints):
        if self._started or not isinstance(endpoints, tuple) or len(endpoints) != 2:
            raise ValueError("one PAO and one FC endpoint are required")
        self._endpoints = list(endpoints)
        try:
            for endpoint in endpoints:
                await endpoint.start()
            self._started = True
        except BaseException:
            await self.close()
            raise

    async def close(self):
        self._started = False
        failures = []
        for binding in tuple(self.runs._active.values()):
            try:
                await self.runs.close_execution(binding=binding)
            except BaseException as error:
                failures.append(error)
        closer = getattr(self._factory, "close", None)
        if callable(closer):
            try:
                await closer()
            except BaseException as error:
                failures.append(error)
        for endpoint in reversed(self._endpoints):
            try:
                await endpoint.close()
            except BaseException as error:
                failures.append(error)
        self._endpoints.clear()
        if failures:
            raise OSError("permission service cleanup is unverified") from failures[0]
        lock = getattr(self, "_installation_lock", None)
        if lock is not None:
            lock.close()
        return True

    def health(self, *, instance_id):
        if instance_id != self.store.instance_id or not self._started:
            raise PermissionDenied("permission service is not ready for this instance")
        return {"instance_id": self.store.instance_id, "generation": self.generation,
                "state": "ready", "levels": list(self.levels)}

    async def open_execution(self, **arguments):
        callbacks = arguments.pop("callbacks", None)
        if callbacks is not None:
            from orchestrator.permissions.ipc_rpc import current_peer

            peer = current_peer()
            if (not isinstance(callbacks, dict) or peer is None
                    or callbacks.get("process_id") != peer.process_id):
                raise PermissionDenied("HER callback does not belong to the authenticated Worker")
        binding = await self.runs.open_execution(**arguments)
        if callbacks is not None:
            self.runs.execution_registry(binding).attach_callbacks(callbacks)
        return binding

    def execution_descriptor(self, *, binding):
        executor = self.runs.execution_registry(binding)
        value = executor.descriptor()
        if not isinstance(value, dict) or value.get("binding") != binding:
            raise PermissionDenied("invalid installed execution descriptor")
        return value

    async def close_execution(self, *, binding, park=False):
        if type(park) is not bool:
            raise PermissionDenied("invalid executor closure")
        if park:
            result = self.runs.execution_registry(binding).park()
            if inspect.isawaitable(result):
                result = await result
            if result is not True:
                raise PermissionDenied("parked resources are not stopped")
            return True
        await self.runs.close_execution(binding=binding)
        return True

    async def settle_run(self, *, binding):
        return await self.runs.settle_run(binding=binding)

    def _context(self, context):
        route = self.runs.parking.route(context.run_id, owner_id=context.owner_id)
        if route is None or binding_from_route(route).context != context:
            raise PermissionDenied("permission route is not owned by this execution")
        return context

    def context_status(self, *, context):
        return self.authority.execution_status(self._context(context))

    def request_state(self, *, context, request_id):
        return self.authority.get_request(self._context(context), request_id)

    def fence_context(self, *, context):
        # Fencing is exact-epoch-only. A late Worker may close its recorded old
        # epoch after PAO registered a replacement; it cannot fence that new one.
        return self.authority.fence_execution(context)

    async def configure_inference(self, *, binding, config):
        from orchestrator.permissions.host_protocol import unpack

        executor = self.runs.execution_registry(binding)
        return await executor.configure_inference(unpack(config))

    async def run_her(self, *, binding, command):
        executor = self.runs.execution_registry(binding)
        # The actual fixed host launcher, not this application, owns OS
        # qualification and rejects a missing/mismatched L2 installation.
        return await executor.run_restricted_her(command)

    def get_session_request(self, subject, request_id):
        return self.control.get_session_request(subject, request_id)

    def list_session_requests(self, subject, *, before=None, limit=10):
        return self.control.list_session_requests(subject, before=before, limit=limit)

    def withdraw_session_request(self, subject, request_id, expected_revision):
        return self.control.withdraw_session_request(subject, request_id, expected_revision)

    def issue_session_actions(self, subject, request_id, revision, *, session_token, presentation_id):
        return self.control.issue_session_actions(
            subject, request_id, revision, session_token=session_token, presentation_id=presentation_id,
        )

    def submit_session_action(self, subject, action_id, *, session_token):
        return self.control.submit_session_action(subject, action_id, session_token=session_token)

    def submit_session_decision(self, subject, arguments, *, session_token, invocation_id, connection_binding):
        return self.control.submit_session_decision(
            subject, arguments, session_token=session_token,
            invocation_id=invocation_id, connection_binding=connection_binding,
        )

    def login(self, *, org_id, email, password):
        # API keys, shared instance bearers and ordinary question answers never
        # reach the human session issuer. Provisioning is an operator action.
        if (not all(isinstance(value, str) and value for value in (org_id, email, password))
                or len(email) > 320 or len(password) > 4096 or len(org_id) > 128):
            raise PermissionDenied("invalid human login")
        user = self.identity.authenticate_user(org_id=org_id, email=email, password=password)
        if user is None or user.id not in self.owners:
            raise PermissionDenied("invalid human login")
        session = self.identity.create_session(user_id=user.id)
        return {"ok": True, "session": {"token": session.token, "expires_at": session.expires_at},
                "user": {"id": user.id, "org_id": user.org_id, "email": user.email,
                         "display_name": user.display_name}}

    def logout(self, *, session_token):
        if not isinstance(session_token, str) or not session_token:
            return {"ok": True, "revoked": False}
        return {"ok": True, "revoked": self.identity.revoke_session(session_token)}

    def session_user(self, *, session_token):
        user = self.identity.get_session_user(session_token)
        if user is None or user.id not in self.owners:
            raise PermissionDenied("human session is unavailable")
        return {"id": user.id, "owner_id": self.owners[user.id], "org_id": user.org_id}

    async def manage_session(self, *, subject, arguments, session_token, invocation_id):
        if self._management is None:
            raise PermissionDenied("permission management is unavailable")
        return await self._management.manage(
            subject, arguments, session_token=session_token, invocation_id=invocation_id,
        )

    def session_summary(self, *, subject):
        return self.authority.session_summary(subject)
