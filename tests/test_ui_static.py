"""Console files stay inside the public build directory, including unauthenticated requests."""
from __future__ import annotations

import os
from urllib.parse import quote

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient


@pytest.fixture
def console(monkeypatch, tmp_path):
    from mnemotrace.server import app as appmod

    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("SYNTHETIC_CONSOLE", encoding="utf-8")
    (dist / "assets" / "app.js").write_text("/* synthetic asset */", encoding="utf-8")
    (dist / "LICENSE.txt").write_text("SYNTHETIC_LICENSE", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("SYNTHETIC_PRIVATE_FILE", encoding="utf-8")
    monkeypatch.setattr(appmod, "_DIST", str(dist))
    # No lifespan: static requests must neither initialize memory nor require credentials.
    client = TestClient(appmod.app, follow_redirects=False)
    try:
        yield client, dist, outside
    finally:
        client.close()


@pytest.mark.parametrize("suffix", [
    "%2e%2e/outside.txt",
    "%2e%2e%2foutside.txt",
    "..%5coutside.txt",
    "%2e%2e%5coutside.txt",
    "%2fetc/passwd",
    "C:%5csynthetic%5coutside.txt",
    "C:outside.txt",
    "%5c%5csynthetic-host%5cshare%5coutside.txt",
    "outside.txt%00",
    "LICENSE.txt:stream",
])
def test_console_rejects_invalid_paths(console, suffix):
    client, _, _ = console
    response = client.get("/ui/" + suffix)
    assert response.status_code == 404
    assert "SYNTHETIC_PRIVATE_FILE" not in response.text
    assert response.headers["x-content-type-options"] == "nosniff"


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_console_rejects_absolute_existing_file(console, method):
    client, _, outside = console
    response = client.request(method, "/ui/" + quote(str(outside), safe=""))
    assert response.status_code == 404


@pytest.mark.parametrize("method", ["GET", "HEAD"])
@pytest.mark.parametrize("path,expected", [
    ("/ui", "SYNTHETIC_CONSOLE"),
    ("/ui/", "SYNTHETIC_CONSOLE"),
    ("/ui/facts", "SYNTHETIC_CONSOLE"),
    ("/ui/conversations/synthetic-session", "SYNTHETIC_CONSOLE"),
    ("/ui/LICENSE.txt", "SYNTHETIC_LICENSE"),
    ("/ui/assets/app.js", "/* synthetic asset */"),
])
def test_console_normal_files_and_navigation(console, method, path, expected):
    client, _, _ = console
    response = client.request(method, path)
    assert response.status_code == 200
    assert response.text == (expected if method == "GET" else "")
    assert response.headers["cache-control"] == (
        "public, max-age=31536000, immutable" if "/assets/" in path else "no-cache"
    )
    assert response.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize("location", ["linked.txt", "assets/linked.txt", "index.html"])
def test_console_rejects_external_file_links(console, location):
    client, dist, outside = console
    link = dist / location
    if link.exists():
        link.unlink()
    try:
        link.symlink_to(outside)
    except OSError as exc:
        pytest.skip(f"file symlink unavailable: {exc}")
    response = client.get("/ui/" + ("facts" if location == "index.html" else location))
    assert response.status_code == 404
    assert "SYNTHETIC_PRIVATE_FILE" not in response.text


def test_console_rejects_directory_link(console):
    client, dist, outside = console
    # This sibling shares the string prefix "dist", but is outside the public directory.
    external = dist.parent / "dist-private"
    external.mkdir()
    (external / outside.name).write_text("SYNTHETIC_PRIVATE_FILE", encoding="utf-8")
    link = dist / "linked-directory"
    if os.name == "nt":
        # Junctions do not require Windows' optional symlink privilege.
        import subprocess

        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(external)],
                       check=True, capture_output=True)
    else:
        link.symlink_to(external, target_is_directory=True)
    try:
        response = client.get("/ui/linked-directory/outside.txt")
        assert response.status_code == 404
        assert "SYNTHETIC_PRIVATE_FILE" not in response.text
    finally:
        # Remove only the link, never traverse the external target.
        if os.name == "nt":
            link.rmdir()
        else:
            link.unlink()


def test_console_missing_build(monkeypatch, tmp_path):
    from mnemotrace.server import app as appmod

    monkeypatch.setattr(appmod, "_DIST", str(tmp_path / "missing"))
    client = TestClient(appmod.app)
    try:
        for path in ("/", "/ui", "/ui/facts", "/ui/assets/app.js"):
            response = client.get(path)
            assert response.status_code == 503
            assert "pnpm" in response.text
    finally:
        client.close()


def test_console_missing_asset_is_not_spa(console):
    client, _, _ = console
    response = client.get("/ui/assets/missing.js")
    assert response.status_code == 404


def test_console_boundary_over_real_http(console):
    """Exercise URL decoding and file transfer over a real loopback socket."""
    import http.client
    import socket
    import threading
    import time

    uvicorn = pytest.importorskip("uvicorn")
    from mnemotrace.server import app as appmod

    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(appmod.app, lifespan="off", log_level="error"))
    worker = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    worker.start()
    try:
        deadline = time.monotonic() + 5
        while not server.started and worker.is_alive() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert server.started
        for method, path, expected_status, expected_body in [
            ("GET", "/ui/%2e%2e/outside.txt", 404, None),
            ("GET", "/ui/..%5coutside.txt", 404, None),
            ("HEAD", "/ui/%2e%2e/outside.txt", 404, b""),
            ("GET", "/ui/facts", 200, b"SYNTHETIC_CONSOLE"),
            ("GET", "/ui/assets/app.js", 200, b"/* synthetic asset */"),
        ]:
            connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            try:
                connection.request(method, path)
                response = connection.getresponse()
                body = response.read()
                assert response.status == expected_status
                assert b"SYNTHETIC_PRIVATE_FILE" not in body
                assert response.getheader("X-Content-Type-Options") == "nosniff"
                if expected_body is not None:
                    assert body == expected_body
            finally:
                connection.close()
    finally:
        server.should_exit = True
        worker.join(timeout=5)
        listener.close()
        assert not worker.is_alive()
