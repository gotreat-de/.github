"""The deploy script against an in-process fake of the Dokploy API and the service's /health."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / ".github" / "scripts" / "deploy-dokploy.sh"
API_KEY = "test-key-123"
RELEASED_COMPOSE = """services:
  app:
    image: ghcr.io/example/app:v1.2.3 # x-release-please-version
  worker:
    image: ghcr.io/example/app:v1.2.3 # x-release-please-version
    command: [ "python", "-m", "app.workers.housekeeping" ]
"""
STALE_COMPOSE = """services:
  worker:
    image: ghcr.io/example/app:latest
    command: [ "python", "-m", "app.workers.reaper" ]
"""

# The module-level skip must not hide these tests in CI - only skip locally when a tool is missing.
pytestmark = pytest.mark.skipif(
    "CI" not in os.environ and any(shutil.which(tool) is None for tool in ("bash", "curl", "jq")),
    reason="the deploy script needs bash, curl and jq",
)


@dataclass
class FakeDokploy:
    final_status: str = "done"
    polls_until_final: int = 2
    health_version: str = "1.2.3"
    deployments: list[dict[str, str]] = field(default_factory=list)
    deploy_calls: list[dict[str, Any]] = field(default_factory=list)
    health_calls: int = 0
    polls: int = 0
    deploy_started: bool = False
    # Listings to answer with a transient error once the deployment has started (the wait loop).
    transient_listing_errors: int = 0
    # The compose Dokploy stores and runs - not the repo's, until a deploy replaces it.
    compose_file: str = STALE_COMPOSE
    source_type: str = "raw"
    # Accept compose.update but keep the stored compose, as an API that dropped the field would.
    ignore_compose_updates: bool = False
    calls: list[str] = field(default_factory=list)

    def advance(self) -> None:
        """Each listing moves the new deployment one step closer to its final status."""
        if not self.deployments or self.deployments[0]["deploymentId"] != "dep-new":
            return
        self.polls += 1
        if self.polls >= self.polls_until_final:
            self.deployments[0]["status"] = self.final_status
            if self.final_status == "error":
                self.deployments[0]["errorMessage"] = "migrate exited with code 1"


def _handler(state: FakeDokploy) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            return

        def _send(self, code: int, payload: object) -> None:
            body = json.dumps(payload).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if self.headers.get("x-api-key") == API_KEY:
                return True
            self._send(401, {"message": "Unauthorized"})
            return False

        def do_GET(self) -> None:
            path = urlparse(self.path).path
            state.calls.append(f"GET {path}")
            if path == "/health":
                state.health_calls += 1
                self._send(200, {"status": "ok", "version": state.health_version})
            elif not self._authorized():
                return
            elif path == "/api/deployment.allByCompose":
                if state.deploy_started and state.transient_listing_errors > 0:
                    state.transient_listing_errors -= 1
                    self._send(502, {"message": "Bad Gateway"})
                    return
                state.advance()
                self._send(200, state.deployments)
            elif path == "/api/compose.one":
                # Dokploy answers with the whole service, its env included - the script must never print it.
                compose = {"composeFile": state.compose_file, "sourceType": state.source_type, "env": "SECRET=1"}
                self._send(200, {"composeId": "compose-1", **compose})
            else:
                self._send(404, {"message": "not found"})

        def do_POST(self) -> None:
            if not self._authorized():
                return
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            path = urlparse(self.path).path
            state.calls.append(f"POST {path}")
            if path == "/api/compose.update":
                if not state.ignore_compose_updates:
                    state.compose_file = payload["composeFile"]
                    state.source_type = payload["sourceType"]
                self._send(200, {"composeId": payload["composeId"]})
                return
            if path != "/api/compose.deploy":
                self._send(404, {"message": "not found"})
                return
            state.deploy_calls.append(payload)
            state.deployments.insert(0, {"deploymentId": "dep-new", "status": "running", "errorMessage": ""})
            state.deploy_started = True
            self._send(200, {"success": True})

    return Handler


@pytest.fixture
def dokploy() -> Iterator[tuple[FakeDokploy, str]]:
    state = FakeDokploy(deployments=[{"deploymentId": "dep-old", "status": "done", "errorMessage": ""}])
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(state))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield state, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _run(
    base_url: str, *, health: bool = True, compose: str = RELEASED_COMPOSE, **overrides: str
) -> subprocess.CompletedProcess[str]:
    with tempfile.TemporaryDirectory() as directory:
        compose_file = Path(directory) / "compose.yml"
        compose_file.write_text(compose)
        return _run_script(base_url, health=health, COMPOSE_FILE=str(compose_file), **overrides)


def _run_script(base_url: str, *, health: bool, **overrides: str) -> subprocess.CompletedProcess[str]:
    env = {
        "PATH": os.environ["PATH"],
        "DOKPLOY_BASE_URL": base_url,
        "DOKPLOY_API_KEY": API_KEY,
        "DOKPLOY_COMPOSE_ID": "compose-1",
        "RELEASE_VERSION": "1.2.3",
        "HEALTH_URL": f"{base_url}/health" if health else "",
        "DOKPLOY_DEPLOY_TIMEOUT_SECONDS": "5",
        "DOKPLOY_HEALTH_TIMEOUT_SECONDS": "2",
        "DOKPLOY_POLL_INTERVAL_SECONDS": "0.1",
        **overrides,
    }
    return subprocess.run(["bash", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=60, check=False)


def test_deploys_waits_and_verifies_the_version(dokploy):
    state, base_url = dokploy

    result = _run(base_url)

    assert result.returncode == 0, result.stderr
    assert [call["composeId"] for call in state.deploy_calls] == ["compose-1"]
    assert "1.2.3" in state.deploy_calls[0]["title"]
    assert "(answer: success)" in result.stdout
    assert state.polls >= state.polls_until_final
    assert state.health_calls >= 1
    assert API_KEY not in result.stdout + result.stderr


def test_reports_the_deployment_to_github_actions(dokploy, tmp_path):
    _, base_url = dokploy
    output, summary = tmp_path / "output", tmp_path / "summary"

    result = _run(base_url, GITHUB_OUTPUT=str(output), GITHUB_STEP_SUMMARY=str(summary))

    assert result.returncode == 0, result.stderr
    assert output.read_text() == "deployment_id=dep-new\n"
    assert "| Version | v1.2.3 |" in summary.read_text()
    assert "| Dokploy deployment | dep-new |" in summary.read_text()


def test_a_failed_deployment_fails_the_script_with_dokploys_message(dokploy):
    state, base_url = dokploy
    state.final_status = "error"

    result = _run(base_url)

    assert result.returncode != 0
    assert "migrate exited with code 1" in result.stderr
    assert state.health_calls == 0


def test_a_transient_api_error_while_waiting_is_retried(dokploy):
    state, base_url = dokploy
    state.transient_listing_errors = 2

    result = _run(base_url)

    assert result.returncode == 0, result.stderr
    assert "warning:" in result.stderr
    assert "HTTP 502" in result.stderr
    assert state.health_calls >= 1


def test_a_cancelled_deployment_without_a_message_fails_clearly(dokploy):
    state, base_url = dokploy
    state.final_status = "cancelled"

    result = _run(base_url)

    assert result.returncode != 0
    assert "cancelled" in result.stderr
    assert "no error message returned" in result.stderr


def test_an_old_version_on_health_fails_after_the_timeout(dokploy):
    state, base_url = dokploy
    state.health_version = "1.2.2"

    result = _run(base_url)

    assert result.returncode != 0
    assert "1.2.3" in result.stderr
    assert state.health_calls >= 1


def test_refuses_to_start_while_another_deployment_is_running(dokploy):
    state, base_url = dokploy
    state.deployments[0]["status"] = "running"

    result = _run(base_url)

    assert result.returncode != 0
    assert "already running" in result.stderr
    assert state.deploy_calls == []


def test_without_a_health_url_the_deployment_status_is_enough(dokploy):
    state, base_url = dokploy

    result = _run(base_url, health=False)

    assert result.returncode == 0, result.stderr
    assert state.health_calls == 0


def test_dry_run_only_reads(dokploy):
    state, base_url = dokploy

    result = _run(base_url, DRY_RUN="true")

    assert result.returncode == 0, result.stderr
    assert state.deploy_calls == []
    assert [call for call in state.calls if call.startswith("POST")] == []
    assert state.compose_file == STALE_COMPOSE
    assert "stores another compose file (source raw)" in result.stdout


def test_dry_run_reports_a_stored_compose_that_is_already_the_released_one(dokploy):
    state, base_url = dokploy
    state.compose_file = RELEASED_COMPOSE + "\n"

    result = _run(base_url, DRY_RUN="true")

    assert result.returncode == 0, result.stderr
    assert "stores the released compose file" in result.stdout


def test_stores_the_released_compose_before_it_deploys(dokploy):
    state, base_url = dokploy
    state.source_type = "github"

    result = _run(base_url)

    assert result.returncode == 0, result.stderr
    assert state.compose_file == RELEASED_COMPOSE
    assert state.source_type == "raw"
    posts = [call for call in state.calls if call.startswith("POST")]
    assert posts == ["POST /api/compose.update", "POST /api/compose.deploy"]
    assert "SECRET=1" not in result.stdout + result.stderr


def test_a_compose_dokploy_did_not_store_stops_before_the_deploy(dokploy):
    state, base_url = dokploy
    state.ignore_compose_updates = True

    result = _run(base_url)

    assert result.returncode != 0
    assert "did not store the released compose file" in result.stderr
    assert state.deploy_calls == []


@pytest.mark.parametrize(
    "compose",
    [
        STALE_COMPOSE,
        RELEASED_COMPOSE.replace("v1.2.3", "v1.2.2"),
        RELEASED_COMPOSE.replace(":v1.2.3", ":v1.2.3-rc.1"),
        "",
    ],
    ids=["latest", "older-version", "other-prerelease", "empty"],
)
def test_a_compose_without_the_released_image_is_refused_before_any_call(dokploy, compose):
    state, base_url = dokploy

    result = _run(base_url, compose=compose)

    assert result.returncode != 0
    assert "COMPOSE_FILE" in result.stderr
    assert state.calls == []


def test_a_wrong_api_key_is_reported(dokploy):
    _, base_url = dokploy

    result = _run(base_url, DOKPLOY_API_KEY="wrong")

    assert result.returncode != 0
    assert "HTTP 401" in result.stderr


def test_a_plain_http_dokploy_url_is_rejected():
    result = _run("http://dokploy.example.com")

    assert result.returncode != 0
    assert "HTTPS" in result.stderr
