"""HTTP and credential helpers for ARC-Bench.

The platform contract implemented here was verified against the public
ARC-Bench deployment:

    GET  /api/auth/me
    GET  /api/competitions
    GET  /api/competitions/{id}
    GET  /api/requirements/{task_id}/tests?catalog=competition
    POST /api/submissions
    POST /api/runs
    POST /api/runs/{id}/start
    GET  /api/runs/{id}

Credentials are read from a local env file and are never written to output
records. The module intentionally uses the Python standard library for its
network and archive handling.
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any


DEFAULT_BASE_URL = "https://arc-bench.com"
DEFAULT_API_BASE_URL = "https://api.arc-bench.com/v1"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_SUBMISSIONS = 4
DEFAULT_MIN_INTERVAL = 30.0
DEFAULT_QUEUE_TIMEOUT = 3600.0
DEFAULT_RETRY_AFTER = 30.0

TERMINAL_STATES = {
    "PASSED",
    "FAILED",
    "ERROR",
    "CANCELLED",
    "CANCELED",
    "TIMEOUT",
}


def _user_config_env() -> Path:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")
    )
    return config_home / "arcbench" / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load env values without making the CLI depend on a lab checkout.

    Lookup order for an explicit call:

    1. the path passed by the caller;
    2. ``ARCBENCH_ENV_FILE``;
    3. ``.env`` in the current directory;
    4. ``.env`` at the standalone source root;
    5. ``~/.config/arcbench/.env``.

    Real environment variables with the ``ARC_BENCH_`` prefix take precedence
    over values from the file. This lets CI provide credentials without
    creating a file on disk.
    """
    values: dict[str, str] = {}
    if path is None:
        candidates = [
            os.environ.get("ARCBENCH_ENV_FILE"),
            Path.cwd() / ".env",
            Path(__file__).resolve().parents[2] / ".env",
            _user_config_env(),
        ]
        path = next(
            (Path(candidate) for candidate in candidates if candidate and Path(candidate).is_file()),
            None,
        )
    if path is not None and path.is_file():
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip("'\"")
    for key, value in os.environ.items():
        if key.startswith("ARC_BENCH_"):
            values[key] = value
    return values


def redact(value: str | None) -> str | None:
    """Return a deliberately non-reconstructable display form."""
    if not value:
        return value
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def sanitize(value: Any) -> Any:
    """Remove credential-shaped fields before a record reaches disk."""
    sensitive = (
        "key",
        "token",
        "cookie",
        "secret",
        "password",
        "authorization",
        "session",
    )
    token_metrics = {
        "tokens",
        "token_count",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "reasoning_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
    }

    def is_sensitive(key: str) -> bool:
        normalized = key.lower().replace("-", "_")
        if normalized in token_metrics:
            return False
        return any(word in normalized for word in sensitive)

    if isinstance(value, dict):
        return {
            key: "***" if is_sensitive(key) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(path: Path) -> dict[str, Any]:
    """Validate a submission archive before any network write occurs."""
    import zipfile

    if not path.is_file():
        raise ValueError(f"submission package not found: {path}")
    if path.suffix.lower() != ".zip":
        raise ValueError("submission package must be a .zip file")
    if path.stat().st_size == 0:
        raise ValueError("submission package is empty")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
    except zipfile.BadZipFile as error:
        raise ValueError(f"submission package is not a valid ZIP: {path}") from error
    if not {"main.py", "index.js", "index.ts"} & set(names):
        raise ValueError("submission package has no root entrypoint (main.py / index.js / index.ts)")
    if any(name.startswith(("/", "\\")) or ".." in Path(name).parts for name in names):
        raise ValueError("submission package contains unsafe archive paths")
    return {"path": str(path), "bytes": path.stat().st_size, "sha256": sha256_file(path)}


def build_multipart(
    fields: dict[str, str],
    file_field: str | None = None,
    file_path: Path | None = None,
    boundary: str | None = None,
) -> tuple[bytes, str]:
    boundary = boundary or f"----arcbench{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    if file_field and file_path:
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{file_path.name}\"\r\nContent-Type: application/zip\r\n\r\n".encode()
        )
        chunks.append(file_path.read_bytes())
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def unwrap(payload: Any, key: str) -> dict[str, Any]:
    """Handle the platform's nested write responses."""
    if isinstance(payload, dict) and isinstance(payload.get(key), dict):
        return payload[key]
    return payload if isinstance(payload, dict) else {"response": payload}


def parse_retry_after(
    headers: dict[str, str] | None,
    default: float = DEFAULT_RETRY_AFTER,
) -> float:
    """Return a non-negative wait duration from a Retry-After header."""
    if not headers:
        return default
    value = headers.get("retry-after")
    if value is None:
        return default
    value = str(value).strip()
    if not value:
        return default
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        parsed = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        parsed = None
    if parsed is None:
        return default
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


def queue_wait_seconds(attempt: int, retry_after: float) -> float:
    """Add a small deterministic backoff and a small random jitter."""
    attempt = max(1, int(attempt))
    base = max(0.0, float(retry_after))
    backoff = base * (1.0 + min(attempt - 1, 5) * 0.1)
    jitter = random.uniform(0.0, min(3.0, base * 0.1))
    return max(0.0, backoff + jitter)


@dataclass
class SubmitConfig:
    base_url: str = DEFAULT_BASE_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    session_cookie: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_submissions: int = DEFAULT_MAX_SUBMISSIONS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL
    timeout_seconds: int = DEFAULT_TIMEOUT
    queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SubmitConfig":
        return cls(
            base_url=env.get("ARC_BENCH_WEB_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_base_url=env.get("ARC_BENCH_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            session_cookie=env.get("ARC_BENCH_SESSION_COOKIE", ""),
            api_key=env.get("ARC_BENCH_API_KEY", ""),
            model=env.get("ARC_BENCH_MODEL", DEFAULT_MODEL),
            max_submissions=int(env.get("ARC_BENCH_MAX_SUBMISSIONS", DEFAULT_MAX_SUBMISSIONS)),
            min_interval_seconds=float(
                env.get("ARC_BENCH_MIN_INTERVAL_SECONDS", DEFAULT_MIN_INTERVAL)
            ),
            timeout_seconds=int(env.get("ARC_BENCH_HTTP_TIMEOUT_SECONDS", DEFAULT_TIMEOUT)),
            queue_timeout_seconds=float(
                env.get("ARC_BENCH_QUEUE_TIMEOUT_SECONDS", DEFAULT_QUEUE_TIMEOUT)
            ),
        )


class RunStartError(RuntimeError):
    """A single /start attempt returned an unsuccessful status."""

    def __init__(
        self,
        run_id: str,
        status: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.status = status
        self.payload = payload
        self.headers = headers or {}
        super().__init__(f"run {run_id} start failed ({status}): {payload}")


class RunQueueTimeoutError(RuntimeError):
    """The run stayed behind the platform capacity gate until timeout."""

    def __init__(
        self,
        run_id: str,
        timeout_seconds: float,
        attempts: int,
        last_status: str,
        last_payload: Any,
    ) -> None:
        self.run_id = run_id
        self.timeout_seconds = timeout_seconds
        self.attempts = attempts
        self.last_status = last_status
        self.last_payload = last_payload
        detail = f"last_status={last_status}"
        if last_payload is not None:
            detail += f" last_response={json.dumps(last_payload, ensure_ascii=False)[:300]}"
        super().__init__(
            f"run {run_id} did not start within {timeout_seconds:.0f}s "
            f"({attempts} attempts, {detail}); recover with "
            f"`arcbench start --run-id {run_id}`"
        )


def _normalize_headers(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


class OfficialClient:
    def __init__(self, config: SubmitConfig):
        self.config = config
        self.last_response_headers: dict[str, str] = {}

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        headers = {
            "Accept": "application/json",
            "User-Agent": "arcbench-cli/0.1",
        }
        if self.config.session_cookie:
            headers["Cookie"] = self.config.session_cookie
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if data is not None and content_type:
            request.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(request, timeout=self.config.timeout_seconds) as response:
                self.last_response_headers = _normalize_headers(response.headers)
                body = response.read()
                try:
                    return response.status, json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    return response.status, body.decode("utf-8", "replace")[:2000]
        except urllib.error.HTTPError as error:
            self.last_response_headers = _normalize_headers(error.headers)
            raw = error.read().decode("utf-8", "replace")
            try:
                return error.code, json.loads(raw)
            except json.JSONDecodeError:
                return error.code, raw[:2000]

    # --- reads -----------------------------------------------------------

    def check_login(self) -> dict[str, Any]:
        if not self.config.session_cookie:
            return {
                "logged_in": False,
                "reason": "ARC_BENCH_SESSION_COOKIE is not configured",
            }
        status, payload = self._request("GET", f"{self.config.base_url}/api/auth/me")
        result: dict[str, Any] = {"logged_in": 200 <= status < 300, "http_status": status}
        if isinstance(payload, dict) and isinstance(payload.get("user"), dict):
            result["username"] = payload["user"].get("username")
        return result

    def list_competitions(self) -> list[dict[str, Any]]:
        status, payload = self._request("GET", f"{self.config.base_url}/api/competitions")
        if not 200 <= status < 300:
            raise RuntimeError(f"competition list failed ({status}): {payload}")
        return payload if isinstance(payload, list) else []

    def get_competition(self, competition_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "GET", f"{self.config.base_url}/api/competitions/{competition_id}"
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"competition lookup failed ({status}): {payload}")
        return payload if isinstance(payload, dict) else {}

    def list_tasks(self, competition_id: str) -> list[dict[str, Any]]:
        payload = self.get_competition(competition_id)
        tasks = payload.get("tasks") if isinstance(payload, dict) else None
        return tasks if isinstance(tasks, list) else []

    def get_test_pack(self, task_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "GET",
            f"{self.config.base_url}/api/requirements/{task_id}/tests?catalog=competition",
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"test pack lookup failed ({status}): {payload}")
        return payload if isinstance(payload, dict) else {}

    def get_run(self, run_id: str) -> dict[str, Any]:
        status, payload = self._request("GET", f"{self.config.base_url}/api/runs/{run_id}")
        if not 200 <= status < 300:
            raise RuntimeError(f"run lookup failed ({status}): {payload}")
        return payload if isinstance(payload, dict) else {}

    def get_run_logs(self, run_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "GET", f"{self.config.base_url}/api/runs/{run_id}/logs"
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"run logs lookup failed ({status}): {payload}")
        if isinstance(payload, dict):
            return payload
        return {"stdout": str(payload)}

    def list_runs(self, limit: int = 10) -> list[dict[str, Any]]:
        status, payload = self._request("GET", f"{self.config.base_url}/api/runs")
        if not 200 <= status < 300:
            raise RuntimeError(f"run list failed ({status}): {payload}")
        return (payload if isinstance(payload, list) else [])[:limit]

    # --- writes ----------------------------------------------------------

    def create_submission(
        self,
        package: Path,
        competition_id: str,
        name: str,
        model: str,
    ) -> dict[str, Any]:
        fields = {
            "runtime": "python",
            "catalog": "competition",
            "competition_id": competition_id,
            "api_key": self.config.api_key,
            "display_name": name,
            "model_name": model,
            "base_url": self.config.api_base_url,
        }
        body, content_type = build_multipart(fields, "file", package)
        status, payload = self._request(
            "POST", f"{self.config.base_url}/api/submissions", body, content_type
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"submission failed ({status}): {payload}")
        return unwrap(payload, "submission")

    def create_run(self, submission_id: str, requirement_id: str) -> dict[str, Any]:
        body, content_type = build_multipart(
            {
                "submission_id": submission_id,
                "requirement_id": requirement_id,
            }
        )
        status, payload = self._request(
            "POST", f"{self.config.base_url}/api/runs", body, content_type
        )
        if not 200 <= status < 300:
            raise RuntimeError(f"run creation failed ({status}): {payload}")
        run = unwrap(payload, "run")
        run_id = run.get("id") or run.get("run_id")
        if not run_id:
            raise RuntimeError(
                "run creation response had no id: "
                + json.dumps(run, ensure_ascii=False)[:300]
            )
        # Creating a run only persists it as PENDING. Starting is intentionally
        # a separate call so a 429 queue wait can retry the same run id without
        # creating another submission or run.
        return run

    def start_run(self, run_id: str) -> dict[str, Any]:
        status, payload = self._request(
            "POST", f"{self.config.base_url}/api/runs/{run_id}/start"
        )
        if not 200 <= status < 300:
            raise RunStartError(
                str(run_id),
                status,
                payload,
                self.last_response_headers,
            )
        return unwrap(payload, "run")

    def _queue_last_status(self, run_id: str, error: RunStartError) -> str:
        try:
            run = self.get_run(run_id)
            status = run.get("status") if isinstance(run, dict) else None
            if status:
                return str(status)
        except Exception:
            pass
        if isinstance(error.payload, dict) and error.payload.get("status"):
            return str(error.payload["status"])
        return "PENDING"

    def start_run_with_queue_wait(
        self,
        run_id: str,
        timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT,
        on_wait=None,
    ) -> dict[str, Any]:
        """Start one run, retrying only HTTP 429 capacity rejections."""
        timeout_seconds = float(timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        attempts = 0

        while True:
            attempts += 1
            try:
                return self.start_run(run_id)
            except RunStartError as error:
                if error.status != 429:
                    raise
                if time.monotonic() >= deadline:
                    raise RunQueueTimeoutError(
                        run_id,
                        timeout_seconds,
                        attempts,
                        self._queue_last_status(run_id, error),
                        error.payload,
                    ) from error

                retry_after = parse_retry_after(error.headers)
                delay = queue_wait_seconds(attempts, retry_after)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunQueueTimeoutError(
                        run_id,
                        timeout_seconds,
                        attempts,
                        self._queue_last_status(run_id, error),
                        error.payload,
                    ) from error
                if delay > remaining:
                    delay = remaining
                if on_wait:
                    on_wait(attempts, retry_after, delay, error.status, error.payload)
                time.sleep(delay)

    def poll_run(
        self,
        run_id: str,
        interval: float = 5.0,
        timeout: float = 1800.0,
        on_tick=None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        last: dict[str, Any] = {}
        while True:
            last = self.get_run(run_id)
            state = str(last.get("status", "")).upper()
            if on_tick:
                on_tick(last)
            if state in TERMINAL_STATES:
                return last
            if time.monotonic() >= deadline:
                last["poll_timed_out"] = True
                return last
            time.sleep(interval)


def progress_line(run: dict[str, Any]) -> str:
    """Render one compact progress line for polling."""
    steps = run.get("steps")
    if isinstance(steps, list) and steps:
        done = sum(1 for step in steps if str(step.get("status")) == "success")
        active = next(
            (step for step in steps if str(step.get("status")) in {"running", "info"}),
            None,
        )
        current = active or (steps[done] if done < len(steps) else steps[-1])
        return (
            f"status={run.get('status')} step={current.get('key')} "
            f"({current.get('title')}) {done}/{len(steps)}"
        )
    return (
        f"status={run.get('status')} passed={run.get('passed_count')} "
        f"failed={run.get('failed_count')}"
    )


def enforce_budget(
    record_dir: Path,
    config: SubmitConfig,
    dry_run: bool = False,
) -> None:
    """Cap how many real submissions one machine can create, and pace them."""
    records = sorted(record_dir.glob("*.json")) if record_dir.exists() else []
    if dry_run:
        return
    if len(records) >= config.max_submissions:
        raise RuntimeError(
            f"submission budget exhausted: {len(records)}/{config.max_submissions}"
        )
    if records:
        newest = max(item.stat().st_mtime for item in records)
        remaining = config.min_interval_seconds - (time.time() - newest)
        if remaining > 0:
            raise RuntimeError(f"throttle active; retry in {remaining:.1f}s")


def write_record(path: Path, record: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize(record), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    """Normalize a run object into the shared metric triple."""
    return {
        "status": run.get("status"),
        "passed": run.get("passed_count"),
        "failed": run.get("failed_count"),
        "pass_rate": run.get("test_pass_rate", run.get("score")),
        "tokens": run.get("token_count"),
        "duration_seconds": run.get("run_duration_seconds"),
        "failure_reason": run.get("failure_reason"),
        "run_id": run.get("id"),
        "submission_id": run.get("submission_id"),
        "requirement_id": run.get("requirement_id"),
        "finished_at": run.get("finished_at"),
    }


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
