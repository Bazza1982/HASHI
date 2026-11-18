"""Optional isolated HASHI -> Remote -> Workbench attachment journey."""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import socket
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import pytest
import uvicorn
from aiohttp import web
from aiohttp.test_utils import TestServer

from orchestrator.session_store import SessionNotFound
from remote.api import server as remote_server
from tests.test_chat_connector_projection import _server
from tests.test_remote_workbench_gateway import _client


def _endpoint_fingerprint(text):
    # The test endpoint is ASCII; exercise the browser's existing routing
    # fingerprint without requiring secrets or any real connection.
    first, second = 2166136261, 2246822507
    for char in text:
        first = ((first ^ ord(char)) * 16777619) & 0xFFFFFFFF
        second = ((second ^ ord(char)) * 3266489909) & 0xFFFFFFFF
    return f'{first:08x}{second:08x}'


@pytest.mark.asyncio
async def test_verified_large_transcript_download_across_real_relays(tmp_path, monkeypatch):
    node = os.environ.get("HASHI_TEST_NODE_EXE")
    workbench = os.environ.get("HASHI_TEST_WORKBENCH_REPO")
    if not node or not workbench:
        pytest.skip("requires explicitly selected disposable Workbench checkout and Node executable")
    node, workbench = Path(node), Path(workbench)
    if not node.is_file() or not (workbench / "server" / "index.js").is_file():
        pytest.fail("selected Workbench checkout or Node executable is unavailable")

    media = tmp_path / "media"
    media.mkdir()
    attachment = media / "large.bin"
    with attachment.open("wb") as output:
        output.write(b"start")
        output.seek(65 * 1024 * 1024)
        output.write(b"tail")
    expected = hashlib.sha256()
    with attachment.open("rb") as source:
        while part := source.read(1024 * 1024):
            expected.update(part)
    digest = expected.hexdigest()
    size = attachment.stat().st_size

    hashi = _server(tmp_path)
    hashi.global_config.base_media_dir = media
    hashi._v1_owner_id = lambda _request: "user:7"

    def visible(**scope):
        if (scope["owner_id"], scope["agent_id"], scope["message_id"], scope["attachment_id"]) != (
            "user:7", "a", "message", "attachment",
        ):
            raise SessionNotFound("visible attachment is unavailable")
        return {
            "local_ref": str(attachment), "mime_type": "application/octet-stream",
            "filename": "large.bin", "size_bytes": size, "sha256": digest,
        }

    hashi.session_store.visible_agent_message_attachment = visible
    app = web.Application()

    async def agents(_request):
        return web.json_response({"agents": [{"name": "a", "is_active": True, "online": True}]})

    app.router.add_get("/api/agents", agents)
    app.router.add_get("/api/transcript/{name}/attachments/{message_id}/{attachment_id}",
                       hashi.handle_transcript_attachment)
    async with TestServer(app) as backend:
        test_client, token = _client(tmp_path)
        monkeypatch.setattr(remote_server, "_workbench_port", backend.port)
        monkeypatch.setattr(remote_server, "local_http_hosts", lambda: ("127.0.0.1",))
        monkeypatch.setattr(remote_server, "_fetch_workbench_health", lambda timeout=1.0: {"ok": True})
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(128)
        remote_port = listener.getsockname()[1]
        config = uvicorn.Config(test_client.app, log_config=None, access_log=False,
                                lifespan="off", loop="asyncio")
        remote = uvicorn.Server(config)
        remote_task = asyncio.create_task(remote.serve(sockets=[listener]))
        process = None
        log_path = tmp_path / "workbench-test.log"
        try:
            async with asyncio.timeout(10):
                while not remote.started:
                    if remote_task.done():
                        await remote_task
                        raise RuntimeError("isolated Remote did not start")
                    await asyncio.sleep(.02)
            reserve = socket.socket()
            reserve.bind(("127.0.0.1", 0))
            workbench_port = reserve.getsockname()[1]
            reserve.close()
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(("HASHI_", "BRIDGE_", "KASUMI_"))
                   and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}}
            home = str(tmp_path / "home")
            Path(home).mkdir()
            env.update(
                USERPROFILE=home, HOME=home, TEMP=home, TMP=home,
                LOCALAPPDATA=home, APPDATA=home, PORT=str(workbench_port),
                HASHI_WORKBENCH_STATE_DIR=str(tmp_path / "wb-state"),
                HASHI_WORKBENCH_DATA_DIR=str(tmp_path / "wb-data"),
                HASHI_WORKBENCH_OBSERVABILITY_DIR=str(tmp_path / "wb-logs"),
                HASHI_WORKBENCH_MDNS_DISCOVERY="0",
                HASHI_REMOTE_SHARED_TOKEN=token,
                NO_PROXY="127.0.0.1,localhost",
                HASHI_WORKBENCH_CONNECTIONS_JSON=json.dumps({
                    "active_connection": "test",
                    "connections": [{
                        "id": "test", "display_name": "Test", "enabled": True,
                        "remote_url": f"http://127.0.0.1:{remote_port}",
                    }],
                }),
            )
            with log_path.open("wb") as log:
                process = await asyncio.create_subprocess_exec(
                    str(node), "server/index.js", cwd=str(workbench), env=env,
                    stdout=log, stderr=asyncio.subprocess.STDOUT,
                )
                origin = f"http://127.0.0.1:{workbench_port}"
                async with aiohttp.ClientSession() as client:
                    async with asyncio.timeout(20):
                        while True:
                            if process.returncode is not None:
                                raise RuntimeError("isolated Workbench exited: " + log_path.read_text(errors="replace"))
                            try:
                                async with client.get(origin + "/api/config") as response:
                                    data = await response.json()
                                    if response.status == 200 and any(
                                        item.get("name") == "a" for item in data.get("agents", [])
                                    ):
                                        break
                            except (aiohttp.ClientError, ValueError):
                                pass
                            await asyncio.sleep(.05)
                    route = origin + "/api/transcript/a/attachments/message/attachment"
                    endpoint = f"http://127.0.0.1:{remote_port}"
                    query = {"download": "1", "source_connection": "test",
                             "source_endpoint": _endpoint_fingerprint(endpoint)}
                    url = route + "?" + urlencode(query)
                    wrong_query = {**query, "source_connection": "different-instance"}
                    async with client.get(route + "?" + urlencode(wrong_query)) as response:
                        assert response.status == 409
                    async with client.get(url) as response:
                        assert response.status == 200, await response.text()
                        assert response.headers["Content-Length"] == str(size)
                        actual, count = hashlib.sha256(), 0
                        async for part in response.content.iter_chunked(1024 * 1024):
                            actual.update(part)
                            count += len(part)
                        assert count == size and actual.hexdigest() == digest
                        etag = response.headers["ETag"]
                    async with client.get(url, headers={"Range": "bytes=0-4", "If-Range": etag}) as response:
                        assert response.status == 206
                        assert await response.read() == b"start"
                        assert response.headers["Content-Range"] == f"bytes 0-4/{size}"
                    async with client.get(origin + "/api/transcript/a/attachments/wrong/attachment?download=1") as response:
                        assert response.status == 404
                    with attachment.open("r+b") as output:
                        output.write(b"wrong")
                    async with client.get(url) as response:
                        assert response.status == 409
        finally:
            if process is not None and process.returncode is None:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), 5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            remote.should_exit = True
            await asyncio.wait_for(remote_task, 10)
            listener.close()
            test_client.close()
