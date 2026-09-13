"""Shared fixtures — most importantly, the live conformance server.

`tests/test_conformance.py` implements every scenario in the monorepo's
`test/conformance/scenarios.json` against the REAL api and worker: the fixture
spawns `node scripts/conformance-server.mjs`, reads the one line of JSON it
prints on stdout when it is accepting connections, and SIGTERMs it afterwards.

Point it at a checkout with `SNAPNEDIT_REPO_ROOT=/path/to/snapnedit`; the
default walks up from this file (the SDK lives at `sdks/python` inside the
monorepo). Without a checkout — or without `node`, or without the monorepo's
`npm run build` output — the conformance tests skip rather than fail, so
`pytest` still works from a standalone clone of the mirror.
"""

from __future__ import annotations

import itertools
import json
import os
import shutil
import signal
import subprocess
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from snapnedit import Snapnedit

READY_TIMEOUT_SECONDS = 180.0
_counter = itertools.count(1)


def repo_root() -> Path | None:
    """Locate the snapnedit monorepo, or `None` when this is a standalone clone."""
    override = os.environ.get("SNAPNEDIT_REPO_ROOT")
    candidates = [Path(override)] if override else []
    candidates.extend(Path(__file__).resolve().parents)
    for candidate in candidates:
        if (candidate / "scripts" / "conformance-server.mjs").is_file():
            return candidate
    return None


def load_scenarios() -> dict[str, Any] | None:
    """Read `test/conformance/scenarios.json` from the monorepo, if there is one."""
    root = repo_root()
    if root is None:
        return None
    path = root / "test" / "conformance" / "scenarios.json"
    if not path.is_file():
        return None
    with path.open(encoding="utf-8") as handle:
        loaded: dict[str, Any] = json.load(handle)
        return loaded


@dataclass
class Conformance:
    """A running conformance server plus the helpers the scenarios need."""

    url: str
    api_key: str
    publishable_key: str
    webhook_secret: str
    account_id: str
    bucket: str
    raw: httpx.Client
    root: Path
    _clients: list[Snapnedit] = field(default_factory=list)

    # -- clients -----------------------------------------------------------

    def client(self, token: str | None = "__api_key__") -> Snapnedit:
        """Build a client: the seeded api key by default, `None` for anonymous."""
        key = self.api_key if token == "__api_key__" else token
        built = Snapnedit(key, self.url, max_retries=0, timeout=30.0)
        self._clients.append(built)
        return built

    def close(self) -> None:
        """Close every client this helper handed out."""
        for built in self._clients:
            built.close()
        self._clients.clear()

    # -- helper endpoints (not part of the api) ----------------------------

    def nonce(self, label: str) -> str:
        """Return a value nothing has submitted before, so a request cannot hit the cache."""
        return f"py-{label}-{os.getpid()}-{next(_counter)}"

    def fixture_bytes(self, name: str = "small.png", nonce: str | None = None) -> bytes:
        """Real image bytes, optionally made byte-unique with `?nonce=`."""
        params = {"nonce": nonce} if nonce else None
        response = self.raw.get(f"/__conformance/fixtures/{name}", params=params)
        assert response.status_code == 200, response.text
        return response.content

    def unique_image(self, label: str) -> bytes:
        """Fixture bytes nobody has submitted before."""
        return self.fixture_bytes("small.png", self.nonce(label))

    def balance(self) -> int:
        """Read the seeded account's live credit balance."""
        response = self.raw.get("/__conformance/credits")
        assert response.status_code == 200, response.text
        balance: int = response.json()["balance"]
        return balance

    def bucket_object(self, key: str) -> dict[str, Any] | None:
        """One object recorded by the stand-in customer bucket, or `None`."""
        response = self.raw.get(f"/__conformance/bucket/{key}")
        if response.status_code != 200:
            return None
        recorded: dict[str, Any] = response.json()
        return recorded

    def destination_body(self, **overrides: Any) -> dict[str, Any]:
        """Arguments for `destinations.create()` pointed at the stand-in bucket."""
        body: dict[str, Any] = {
            "name": f"conformance {self.nonce('dest')}",
            "provider": "s3-compatible",
            "bucket": self.bucket,
            "region": "auto",
            "endpoint": self.url,
            "force_path_style": True,
            "key_prefix": "conformance/",
            "access_key_id": "CONFORMANCEKEYID",
            "secret_access_key": "conformance-secret-access-key",
        }
        body.update(overrides)
        return body


def _read_ready_line(process: subprocess.Popen[str], out: list[str]) -> None:
    assert process.stdout is not None
    line = process.stdout.readline()
    out.append(line)


def _spawn(root: Path) -> tuple[subprocess.Popen[str], dict[str, Any]]:
    env = {**os.environ, "CONFORMANCE_PORT": "0"}
    process = subprocess.Popen(
        ["node", "scripts/conformance-server.mjs"],
        cwd=str(root),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    captured: list[str] = []
    reader = threading.Thread(target=_read_ready_line, args=(process, captured), daemon=True)
    reader.start()
    reader.join(READY_TIMEOUT_SECONDS)
    if not captured or not captured[0].strip():
        _terminate(process)
        raise RuntimeError("the conformance server printed no ready line")
    return process, json.loads(captured[0])


def _terminate(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.send_signal(signal.SIGTERM)
        try:
            process.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - defensive
            process.kill()
            process.wait(timeout=10)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


@pytest.fixture(scope="session")
def conformance() -> Iterator[Conformance]:
    """Start the monorepo's conformance server for the session, and tear it down."""
    root = repo_root()
    if root is None:
        pytest.skip("no snapnedit monorepo checkout (set SNAPNEDIT_REPO_ROOT)")
    if shutil.which("node") is None:
        pytest.skip("node is not on PATH")

    last: Exception | None = None
    for _attempt in range(3):
        try:
            process, config = _spawn(root)
            break
        except Exception as exc:
            last = exc
    else:
        pytest.skip(f"could not start the conformance server ({last}); run `npm run build` first")

    raw = httpx.Client(base_url=config["url"], timeout=30.0)
    helper = Conformance(
        url=config["url"],
        api_key=config["apiKey"],
        publishable_key=config["publishableKey"],
        webhook_secret=config["webhookSecret"],
        account_id=config["accountId"],
        bucket=config.get("bucket", "conformance-bucket"),
        raw=raw,
        root=root,
    )
    try:
        yield helper
    finally:
        helper.close()
        raw.close()
        _terminate(process)


@pytest.fixture(scope="session")
def scenarios() -> dict[str, Any]:
    """Load the language-neutral scenario contract."""
    loaded = load_scenarios()
    if loaded is None:
        pytest.skip("no scenarios.json (set SNAPNEDIT_REPO_ROOT)")
    return loaded
