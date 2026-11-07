"""Installed credential references reach their authorized spaces over stdio."""
import os
import sys

import pytest

from mnemotrace.credentials import store_key


@pytest.mark.asyncio
async def test_revoked_key_is_rejected_on_next_http_and_remote_mcp_request(product_service, spaces, accounts):
    import httpx

    from mnemotrace.mcp.backends import RemoteBackend
    from mnemotrace.server.app import app

    user = accounts.create_user()
    other_user = accounts.create_user()
    try:
        space = spaces.create(user, 'Authorized space')
        other_space = spaces.create(other_user, 'Unauthorized space')
        key = accounts.issue_key(user)
        active_key = accounts.issue_key(user)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            headers = {'Authorization': f'Bearer {key.value}'}
            remote = RemoteBackend('http://test', key.value, space.space_id, client=client)
            params = {'space_id': space.space_id}
            path = '/v1/agent/status'
            assert (await client.get(path, headers=headers, params=params)).status_code == 200
            assert (await remote.agent_status())['space_id'] == space.space_id
            denied = await client.get(path, headers=headers, params={'space_id': other_space.space_id})
            assert denied.status_code == 404

            accounts.revoke_key(user, key.info.key_id)
            assert (await client.get(path, headers=headers, params=params)).status_code == 401
            with pytest.raises(httpx.HTTPStatusError) as error:
                await remote.agent_status()
            assert error.value.response.status_code == 401
            active = RemoteBackend('http://test', active_key.value, space.space_id, client=client)
            assert (await active.agent_status())['space_id'] == space.space_id
    finally:
        accounts.delete_user(user)
        accounts.delete_user(other_user)


@pytest.mark.parametrize("launch_source", ["install", "snippet", "rendered", "toml"])
def test_installed_file_reference_reaches_same_tenant_over_real_stdio(tmp_path, monkeypatch, launch_source, product_service, product_scope):
    import asyncio
    import json
    import re
    import socket
    import threading
    import time

    uvicorn = pytest.importorskip("uvicorn")
    pytest.importorskip("mcp")
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    from mnemotrace.agent_config import load_codex_server
    from mnemotrace.agent_doctor import _codex_config_probe, _mcp_json_config_probe
    from mnemotrace.agent_setup import (
        install_codex_config,
        install_mcp_json_config,
        mcp_args,
        render_mcp_json_config_result,
    )
    from mnemotrace.server import app as appmod

    context, key = product_scope()
    scope = {"space_id": context.space_id}
    credential_directory = "凭据😀𠀀 with spaces" if launch_source == "toml" else "credentials with spaces"
    reference = store_key(key, directory=tmp_path / ".mnemotrace" / credential_directory)
    monkeypatch.chdir(tmp_path)
    relative = os.path.relpath(reference, tmp_path)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    config = tmp_path / ".mcp.json"
    result = install_mcp_json_config(config_path=str(config), api_url=url, api_key_file=relative, python_cmd=sys.executable, **scope)
    args = result["server"]["args"] if launch_source == "install" else mcp_args(
        api_url=url, api_key_file=relative, **scope,
    )
    if launch_source == "rendered":
        output = render_mcp_json_config_result(result, python_cmd=sys.executable, api_url=url, api_key=key, **scope)
        block = re.search(r"```json\n(.*?)```", output, re.S).group(1)
        displayed = json.loads(block)
        assert displayed == result["server"]
        args = displayed["args"]
    if launch_source == "toml":
        config = tmp_path / "config.toml"
        install_codex_config(config_path=str(config), api_url=url, api_key_file=relative, python_cmd=sys.executable, **scope)
        try:
            import tomllib
        except ModuleNotFoundError:
            tomllib = pytest.importorskip("tomli")
        with config.open("rb") as stream:
            parsed = tomllib.load(stream)["mcp_servers"]["mnemotrace"]
        assert parsed == load_codex_server(config)
        args = parsed["args"]
    elsewhere = tmp_path / "another-working-directory"
    elsewhere.mkdir()
    assert "--api-key" not in args
    assert args[args.index("--api-key-file") + 1] == reference
    server = uvicorn.Server(uvicorn.Config(appmod.app, lifespan="off", log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()

    async def call_status():
        params = StdioServerParameters(command=sys.executable, args=args, cwd=str(elsewhere))
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()
                status = await session.call_tool("mnemotrace_agent_status", {"response_format": "json"})
                return json.loads(status.content[0].text)

    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        async def bounded():
            return await asyncio.wait_for(call_status(), timeout=20)
        status = asyncio.run(bounded())
        assert status["space_id"] == context.space_id
        if launch_source == "toml":
            check = _codex_config_probe(str(config), python_cmd=sys.executable, api_url=url, api_key=key, **scope)
        else:
            check = _mcp_json_config_probe(str(config), server_name="mnemotrace", python_cmd=sys.executable, api_url=url, api_key=key, **scope)
        assert check["status"] == "ok" and check["data"]["api_key_ok"] is True
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive()
