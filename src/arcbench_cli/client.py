"""HTTP and credential helpers for ARC-Bench.

The platform contract implemented here was verified against the public
ARC-Bench deployment; the full route table is in the README. Credentials are
read from a local env file and are never written to output records. The module
uses the Python standard library only, for network, archive and crypto work
alike.

Two rules shape the design and are worth stating once:

* A mutation is never retried automatically. When a write's outcome cannot be
  observed, the error says so and names the identifier needed to inspect it.
* Only HTTP 429 capacity rejections are retried, and only for ``/start``, which
  re-uses the same run id instead of creating a second run.
"""

from __future__ import annotations

import hashlib
import http.client
import http.cookiejar
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

DEFAULT_BASE_URL = "https://arc-bench.com"
DEFAULT_API_BASE_URL = "https://api.arc-bench.com/v1"
DEFAULT_METER_BASE_URL = "https://meter.arc-bench.com"
DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_TIMEOUT = 60
DEFAULT_MAX_SUBMISSIONS = 4
DEFAULT_MIN_INTERVAL = 30.0
DEFAULT_QUEUE_TIMEOUT = 3600.0
DEFAULT_RETRY_AFTER = 30.0
DEFAULT_POLL_TOLERANCE = 2

SESSION_COOKIE = "arcbench_session"

TERMINAL_STATES = {"PASSED", "FAILED", "ERROR", "CANCELLED", "CANCELED", "TIMEOUT", "PAUSED"}

RUN_FIELDS = (
    "id",
    "submission_id",
    "requirement_id",
    "competition_id",
    "display_name",
    "model_name",
    "status",
    "score",
    "test_pass_rate",
    "passed_count",
    "failed_count",
    "run_duration_seconds",
    "token_count",
    "feature_implemented_count",
    "feature_total_count",
    "failure_reason",
    "finished_at",
)

SUBMISSION_FIELDS = (
    "id",
    "display_name",
    "model_name",
    "competition_id",
    "requirement_id",
    "runtime",
    "created_at",
)


def _user_config_env() -> Path:
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return config_home / "arcbench" / ".env"


def load_env(path: Path | None = None) -> dict[str, str]:
    """Load env values without making the CLI depend on any particular checkout.

    Lookup order for an implicit call:

    1. ``ARCBENCH_ENV_FILE``;
    2. ``.env`` in the current directory;
    3. ``~/.config/arcbench/.env``.

    Real environment variables with the ``ARC_BENCH_`` prefix take precedence
    over values from the file, so CI can supply credentials without a file.
    """
    values: dict[str, str] = {}
    if path is None:
        candidates = [
            os.environ.get("ARCBENCH_ENV_FILE"),
            Path.cwd() / ".env",
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


_SENSITIVE_WORDS = ("key", "token", "cookie", "secret", "password", "authorization", "session")
_TOKEN_METRICS = {
    "tokens",
    "token_count",
    "total_tokens",
    "input_tokens",
    "output_tokens",
    "reasoning_tokens",
    "cache_read_tokens",
    "cache_write_tokens",
    "token_cost_usd",
    "token_cost_currency",
}
_METER_BASELINE = re.compile(r"(baseline captured for access key)\s+\S+", re.IGNORECASE)


def _is_sensitive(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    if normalized in _TOKEN_METRICS:
        return False
    return any(word in normalized for word in _SENSITIVE_WORDS)


def sanitize(value: Any, secrets: tuple[str, ...] | list[str] = ()) -> Any:
    """Remove credential-shaped fields, and known secret values, from a record.

    Field-name masking alone is not enough: run logs and workspace files quote
    the model access key as free text, so known secret strings are replaced
    wherever they appear.
    """
    if isinstance(value, dict):
        return {
            key: "***" if _is_sensitive(key) else sanitize(item, secrets)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item, secrets) for item in value]
    if isinstance(value, str):
        value = _METER_BASELINE.sub(r"\1 ***", value)
        for secret in secrets:
            if secret:
                value = value.replace(secret, "***")
    return value


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_package(path: Path) -> dict[str, Any]:
    """Validate a submission archive before any network write occurs."""
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
    file_bytes: bytes | None = None,
) -> tuple[bytes, str]:
    boundary = boundary or f"----arcbench{uuid.uuid4().hex}"
    chunks: list[bytes] = []
    for key, value in fields.items():
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{key}\"\r\n\r\n{value}\r\n".encode()
        )
    if file_field and file_path:
        name = file_path.name
        if any(character in name for character in ('"', "\r", "\n")):
            raise ValueError("archive filename contains unsupported characters")
        chunks.append(
            f"--{boundary}\r\nContent-Disposition: form-data; name=\"{file_field}\"; "
            f"filename=\"{name}\"\r\nContent-Type: application/zip\r\n\r\n".encode()
        )
        chunks.append(file_path.read_bytes() if file_bytes is None else file_bytes)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode())
    return b"".join(chunks), f"multipart/form-data; boundary={boundary}"


def unwrap(payload: Any, key: str) -> dict[str, Any]:
    """Handle the platform's nested write responses."""
    if isinstance(payload, dict) and isinstance(payload.get(key), dict):
        return payload[key]
    return payload if isinstance(payload, dict) else {"response": payload}


def parse_retry_after(headers: dict[str, str] | None, default: float = DEFAULT_RETRY_AFTER) -> float:
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


def normalize_task_filter(task: str | None) -> str | None:
    """Match the website's leaderboard selector, which uses the id suffix."""
    if not task or task == "all":
        return None
    return task.split("--")[-1]


class ApiError(RuntimeError):
    """A platform request failed, or its outcome could not be observed."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        method: str | None = None,
        path: str | None = None,
        uncertain: bool = False,
        transport: bool = False,
    ) -> None:
        super().__init__(message)
        self.details = {
            "error": message,
            "http_status": status,
            "method": method,
            "path": path,
            "outcome_uncertain": uncertain,
            "transport_failure": transport,
        }

    @property
    def status(self) -> int | None:
        return self.details["http_status"]

    @property
    def uncertain(self) -> bool:
        return bool(self.details["outcome_uncertain"])

    @property
    def transport(self) -> bool:
        return bool(self.details["transport_failure"])


class RunStartError(ApiError):
    """A single /start attempt returned an unsuccessful status."""

    def __init__(
        self,
        run_id: str,
        status: int,
        payload: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.run_id = run_id
        self.payload = payload
        self.headers = headers or {}
        super().__init__(
            f"run {run_id} start failed ({status}): {payload}",
            status=status,
            method="POST",
            path=f"/runs/{run_id}/start",
            uncertain=status >= 500 or status == 408,
        )


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
            f"`arcbench start {run_id}`"
        )


@dataclass
class SubmitConfig:
    base_url: str = DEFAULT_BASE_URL
    api_base_url: str = DEFAULT_API_BASE_URL
    meter_base_url: str = DEFAULT_METER_BASE_URL
    session_cookie: str = ""
    meter_cookie: str = ""
    api_key: str = ""
    model: str = DEFAULT_MODEL
    max_submissions: int = DEFAULT_MAX_SUBMISSIONS
    min_interval_seconds: float = DEFAULT_MIN_INTERVAL
    timeout_seconds: int = DEFAULT_TIMEOUT
    queue_timeout_seconds: float = DEFAULT_QUEUE_TIMEOUT
    auth_path: str = "/api/auth/me"

    @classmethod
    def from_env(cls, env: dict[str, str]) -> "SubmitConfig":
        return cls(
            base_url=env.get("ARC_BENCH_WEB_BASE_URL", DEFAULT_BASE_URL).rstrip("/"),
            api_base_url=env.get("ARC_BENCH_API_BASE_URL", DEFAULT_API_BASE_URL).rstrip("/"),
            meter_base_url=env.get("ARC_BENCH_METER_BASE_URL", DEFAULT_METER_BASE_URL).rstrip("/"),
            session_cookie=env.get("ARC_BENCH_SESSION_COOKIE", ""),
            meter_cookie=env.get("ARC_BENCH_METER_COOKIE", ""),
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

    def for_meter(self) -> "SubmitConfig":
        """Return a configuration bound to the separate metering service.

        The two services never see each other's cookies.
        """
        return SubmitConfig(
            base_url=self.meter_base_url,
            meter_base_url=self.meter_base_url,
            session_cookie=self.meter_cookie,
            timeout_seconds=self.timeout_seconds,
            auth_path="/api/user/me",
        )


class _SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse to forward an authenticated request to another origin."""

    def __init__(self, origin: str) -> None:
        self.origin = origin

    def redirect_request(self, request, response, code, message, headers, newurl):
        target = urllib.parse.urlparse(newurl)
        path = urllib.parse.urlparse(request.full_url).path
        if f"{target.scheme}://{target.netloc}" != self.origin:
            raise ApiError(
                "refusing to forward this authenticated request to another origin",
                method=request.get_method(),
                path=path,
            )
        if request.get_method() not in ("GET", "HEAD"):
            raise ApiError(
                "mutation redirected; inspect the existing submission or run before retrying",
                method=request.get_method(),
                path=path,
                uncertain=True,
            )
        return super().redirect_request(request, response, code, message, headers, newurl)


def _normalize_headers(headers: Any) -> dict[str, str]:
    return {str(key).lower(): str(value) for key, value in headers.items()}


def parse_cookie_header(header: str) -> list[tuple[str, str]]:
    pairs = []
    for part in header.split(";"):
        name, separator, value = part.strip().partition("=")
        if separator and name:
            pairs.append((name.strip(), value.strip()))
    return pairs


class OfficialClient:
    """A cookie-authenticated client for one ARC-Bench origin."""

    def __init__(self, config: SubmitConfig):
        self.config = config
        self.last_response_headers: dict[str, str] = {}
        self.known_secrets: list[str] = []
        parsed = urllib.parse.urlparse(config.base_url)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("base URL must be an http(s) origin")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.cookies = http.cookiejar.CookieJar()
        self._seed_cookies(parsed.hostname or "", parsed.scheme == "https")
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cookies),
            _SameOriginRedirect(self.origin),
        )

    def _seed_cookies(self, hostname: str, secure: bool) -> None:
        """Load the configured cookie header into a jar.

        A jar rather than a fixed header, because the platform hands out a
        route-affinity cookie that later requests must echo back.
        """
        for name, value in parse_cookie_header(self.config.session_cookie):
            self.cookies.set_cookie(
                http.cookiejar.Cookie(
                    version=0,
                    name=name,
                    value=value,
                    port=None,
                    port_specified=False,
                    domain=hostname,
                    domain_specified=False,
                    domain_initial_dot=False,
                    path="/",
                    path_specified=True,
                    secure=secure,
                    expires=None,
                    discard=True,
                    comment=None,
                    comment_url=None,
                    rest={"HttpOnly": ""},
                )
            )
            if name == SESSION_COOKIE:
                self.known_secrets.append(value)

    def safe(self, value: Any) -> Any:
        secrets = [*self.known_secrets, *(cookie.value for cookie in self.cookies)]
        return sanitize(value, tuple(secrets))

    # --- transport -------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, Any]:
        """Perform one request. Never retries, for any method."""
        headers = {"Accept": "application/json", "User-Agent": "arcbench-cli/0.2"}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        if data is not None and content_type:
            request.add_header("Content-Type", content_type)
        path = urllib.parse.urlparse(url).path
        safe_method = method in ("GET", "HEAD")
        try:
            with self.opener.open(request, timeout=self.config.timeout_seconds) as response:
                self.last_response_headers = _normalize_headers(response.headers)
                body = response.read()
                content = response.headers.get("Content-Type", "")
                if "application/json" not in content and body[:1] not in (b"{", b"["):
                    return response.status, body
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
        except ApiError:
            raise
        except (
            http.client.IncompleteRead,
            http.client.HTTPException,
            urllib.error.URLError,
            OSError,
        ) as error:
            # A truncated body is a transport failure, never a run result.
            raise ApiError(
                self.safe(f"{type(error).__name__}: {error}"),
                method=method,
                path=path,
                uncertain=not safe_method,
                transport=True,
            ) from None

    def request(self, method: str, path: str, data: bytes | None = None, content_type: str | None = None) -> Any:
        """Call one API route and raise ApiError unless the status is 2xx."""
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise ValueError("expected a relative API path")
        status, payload = self._request(method, f"{self.config.base_url}/api{path}", data, content_type)
        if not 200 <= status < 300:
            detail = payload.get("detail", payload) if isinstance(payload, dict) else payload
            message = detail if isinstance(detail, str) else json.dumps(detail, ensure_ascii=False)[:300]
            raise ApiError(
                self.safe(str(message)),
                status=status,
                method=method,
                path=path,
                uncertain=method not in ("GET", "HEAD") and (status >= 500 or status == 408),
            )
        return payload

    def binary(self, path: str) -> bytes:
        payload = self.request("GET", path)
        if not isinstance(payload, (bytes, bytearray)):
            raise ApiError("expected an archive, received JSON", method="GET", path=path)
        return bytes(payload)

    # --- reads -----------------------------------------------------------

    def check_login(self) -> dict[str, Any]:
        if not self.config.session_cookie:
            return {"logged_in": False, "reason": "session cookie is not configured"}
        status, payload = self._request("GET", f"{self.config.base_url}{self.config.auth_path}")
        result: dict[str, Any] = {"logged_in": 200 <= status < 300, "http_status": status}
        user = payload.get("user") if isinstance(payload, dict) else None
        if isinstance(user, dict):
            result["username"] = user.get("username")
            result["registration_source"] = user.get("registration_source")
        account = payload.get("account") if isinstance(payload, dict) else None
        if isinstance(account, dict):
            result["account"] = redact(str(account.get("account_id", "")))
        return result

    def access_key(self) -> str:
        """Read the account's model gateway key and mark it for redaction."""
        payload = self.request("GET", "/auth/access-key")
        key = payload.get("api_key", "") if isinstance(payload, dict) else ""
        if key:
            self.known_secrets.append(key)
        return key

    def list_competitions(self) -> list[dict[str, Any]]:
        payload = self.request("GET", "/competitions")
        return payload if isinstance(payload, list) else []

    def get_competition(self, competition_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/competitions/{quote(competition_id)}")
        return payload if isinstance(payload, dict) else {}

    def list_tasks(self, competition_id: str) -> list[dict[str, Any]]:
        tasks = self.get_competition(competition_id).get("tasks")
        return tasks if isinstance(tasks, list) else []

    def get_test_pack(self, task_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/requirements/{quote(task_id)}/tests?catalog=competition")
        return payload if isinstance(payload, dict) else {}

    def list_submissions(self, competition_id: str | None = None) -> list[dict[str, Any]]:
        payload = self.request("GET", "/submissions")
        items = payload if isinstance(payload, list) else []
        return [
            item
            for item in items
            if not competition_id or item.get("competition_id") == competition_id
        ]

    def get_run(self, run_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/runs/{quote(run_id)}")
        return payload if isinstance(payload, dict) else {}

    def get_run_logs(self, run_id: str, offset: int = 0) -> dict[str, Any]:
        payload = self.request("GET", f"/runs/{quote(run_id)}/logs?log_offset={int(offset)}")
        return payload if isinstance(payload, dict) else {"stdout": str(payload)}

    def get_run_source(self, run_id: str, file_path: str) -> dict[str, Any]:
        query = urllib.parse.urlencode({"file_path": file_path, "kind": "file"})
        payload = self.request("GET", f"/runs/{quote(run_id)}/source?{query}")
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise ApiError("source endpoint returned no text content", method="GET", path="/runs/source")
        return payload

    def list_runs(
        self,
        limit: int = 10,
        task: str | None = None,
        submission: str | None = None,
    ) -> list[dict[str, Any]]:
        query = urllib.parse.urlencode(
            {
                key: value
                for key, value in (("requirement_id", task), ("submission_id", submission))
                if value
            }
        )
        payload = self.request("GET", "/runs" + (f"?{query}" if query else ""))
        return (payload if isinstance(payload, list) else [])[:limit]

    def leaderboard(
        self,
        competition_id: str,
        task: str | None = None,
        team: str | None = None,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """Return ranked rows in the server's own order.

        Positions are assigned before any team filter, so a filtered row keeps
        the rank the board actually gave it. A task board is never silently
        substituted for an aggregate one.
        """
        filters = {"track": "all", "competition_id": competition_id}
        task_id = normalize_task_filter(task)
        if task_id:
            filters["task_id"] = task_id
        payload = self.request("GET", "/competitions/leaderboard?" + urllib.parse.urlencode(filters))
        rows = payload if isinstance(payload, list) else []
        ranked = [{"rank": index + 1, **row} for index, row in enumerate(rows)]
        if team:
            ranked = [row for row in ranked if row.get("username") == team]
        return ranked[:limit]

    def balance(self) -> dict[str, Any]:
        """Read the metering dashboard's balance and billing freshness."""
        balance = self.request("GET", "/user/balance")
        snapshot = balance.get("balance") or {} if isinstance(balance, dict) else {}
        freshness = self.request("GET", "/user/freshness")
        freshness = freshness if isinstance(freshness, dict) else {}
        return {
            # Decimal amounts stay strings; a zero balance is not missing.
            "available_balance": snapshot.get("available_balance", snapshot.get("balance")),
            "currency": (balance.get("currency") if isinstance(balance, dict) else None)
            or snapshot.get("currency"),
            "as_of": freshness.get("as_of"),
            "pending_billing_events": freshness.get("pending_billing_events"),
        }

    def download_submission_archive(self, submission_id: str) -> bytes:
        return self.binary(f"/submissions/{quote(submission_id)}/archive")

    def download_workspace(self, run_id: str) -> bytes:
        return self.binary(f"/runs/{quote(run_id)}/workspace/template-bundle")

    # --- writes ----------------------------------------------------------

    def create_submission(
        self,
        package: Path,
        competition_id: str,
        name: str,
        model: str,
        runtime: str = "python",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        key = api_key if api_key is not None else self.config.api_key
        if not key:
            key = self.access_key()
        if not key:
            raise ValueError("no model gateway key: set ARC_BENCH_API_KEY or use --api-key-env")
        self.known_secrets.append(key)
        fields = {
            "competition_id": competition_id,
            "runtime": runtime,
            "catalog": "competition",
            "agent_source": "upload",
            "display_name": name,
            "base_url": self.config.api_base_url,
            "api_key": key,
            "model": model,
            "model_name": model,
        }
        body, content_type = build_multipart(fields, "file", package)
        payload = self.request("POST", "/submissions", body, content_type)
        return unwrap(payload, "submission")

    def upload(
        self,
        package: Path,
        competition_id: str,
        name: str,
        model: str,
        runtime: str = "python",
        api_key: str | None = None,
    ) -> dict[str, Any]:
        """Save a submission, then verify the stored archive by download.

        A failed or mismatched verification still returns the saved id, so a
        caller never re-uploads to recover one.
        """
        digest = sha256_file(package)
        submission = self.create_submission(package, competition_id, name, model, runtime, api_key)
        result: dict[str, Any] = {
            "submission": self.safe(submission),
            "uploaded_sha256": digest,
            "archive_verified": False,
        }
        submission_id = submission.get("id") or submission.get("submission_id")
        if not submission_id:
            result["archive_error"] = {"error": "submission response had no id"}
            return result
        try:
            downloaded = self.download_submission_archive(str(submission_id))
            result["downloaded_sha256"] = hashlib.sha256(downloaded).hexdigest()
            result["archive_verified"] = result["downloaded_sha256"] == digest
        except ApiError as error:
            result["archive_error"] = error.details
        return result

    def create_run(self, submission_id: str, requirement_id: str) -> dict[str, Any]:
        body, content_type = build_multipart(
            {"submission_id": submission_id, "requirement_id": requirement_id}
        )
        payload = self.request("POST", "/runs", body, content_type)
        run = unwrap(payload, "run")
        if not (run.get("id") or run.get("run_id")):
            raise ApiError(
                "run creation response had no id: " + json.dumps(run, ensure_ascii=False)[:300],
                method="POST",
                path="/runs",
            )
        # Creating a run only persists it as PENDING. Starting is a separate
        # call so a 429 queue wait can retry the same run id.
        return run

    def start_run(self, run_id: str) -> dict[str, Any]:
        status, payload = self._request("POST", f"{self.config.base_url}/api/runs/{quote(run_id)}/start")
        if not 200 <= status < 300:
            raise RunStartError(str(run_id), status, payload, self.last_response_headers)
        return unwrap(payload, "run")

    def cancel_run(self, run_id: str) -> dict[str, Any]:
        """Send exactly one cancellation, then read the resulting state.

        A cancellation that reports HTTP 500 can still have taken effect, so the
        error is surfaced together with the state that follows it.
        """
        result: dict[str, Any] = {"run_id": run_id}
        try:
            self.request("POST", f"/runs/{quote(run_id)}/cancel")
            result["cancel_accepted"] = True
        except ApiError as error:
            result["cancel_accepted"] = False
            result["cancel_error"] = error.details
        result["run"] = summarize_run(self.get_run(run_id))
        return result

    def _queue_last_status(self, run_id: str, error: RunStartError) -> str:
        try:
            status = self.get_run(run_id).get("status")
            if status:
                return str(status)
        except (ApiError, ValueError):
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
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RunQueueTimeoutError(
                        run_id,
                        timeout_seconds,
                        attempts,
                        self._queue_last_status(run_id, error),
                        error.payload,
                    ) from error
                retry_after = parse_retry_after(error.headers)
                delay = min(queue_wait_seconds(attempts, retry_after), remaining)
                if on_wait:
                    on_wait(attempts, retry_after, delay, error.status, error.payload)
                time.sleep(delay)

    def poll_run(
        self,
        run_id: str,
        interval: float = 5.0,
        timeout: float = 1800.0,
        on_tick=None,
        tolerate_failures: int = DEFAULT_POLL_TOLERANCE,
        on_retry=None,
    ) -> dict[str, Any]:
        """Poll one run to a terminal state, or to the local deadline.

        Transport failures and 5xx replies are tolerated up to
        ``tolerate_failures`` consecutively, then re-raised: an observed run
        closed its response mid-body, and a partial read must never be reported
        as a result. Timing out locally does not cancel the remote run.
        """
        if interval <= 0 or timeout <= 0:
            raise ValueError("poll timeout and interval must be positive")
        deadline = time.monotonic() + timeout
        failures = 0
        last: dict[str, Any] = {}
        while True:
            try:
                last = self.get_run(run_id)
            except ApiError as error:
                failures += 1
                recoverable = error.transport or (error.status or 0) >= 500
                if failures > tolerate_failures or not recoverable:
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return {"id": run_id, "status": last.get("status"), "poll_timed_out": True}
                if on_retry:
                    on_retry(failures, error)
                time.sleep(min(interval, remaining))
                continue
            failures = 0
            if on_tick:
                on_tick(last)
            if str(last.get("status", "")).upper() in TERMINAL_STATES:
                return last
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                last["poll_timed_out"] = True
                return last
            time.sleep(min(interval, remaining))


def quote(value: str) -> str:
    return urllib.parse.quote(str(value), safe="")


def progress_line(run: dict[str, Any]) -> str:
    """Render one compact progress line for polling."""
    steps = run.get("steps")
    if isinstance(steps, list) and steps:
        done = sum(1 for step in steps if str(step.get("status")) == "success")
        active = next(
            (step for step in steps if str(step.get("status")) in {"running", "info"}), None
        )
        current = active or (steps[done] if done < len(steps) else steps[-1])
        return (
            f"status={run.get('status')} step={current.get('key')} "
            f"({current.get('title') or current.get('description')}) {done}/{len(steps)}"
        )
    return (
        f"status={run.get('status')} passed={run.get('passed_count')} "
        f"failed={run.get('failed_count')}"
    )


def summarize_run(run: dict[str, Any]) -> dict[str, Any]:
    """Normalize a run object into one summary shape.

    The platform names its cost field ``token_cost_usd`` while returning an
    explicit currency that is not always USD, so the amount and the currency
    are reported together and the field name is not trusted.
    """
    if not isinstance(run, dict):
        return {}
    summary: dict[str, Any] = {
        "run_id": run.get("id"),
        "status": run.get("status"),
        "passed": run.get("passed_count"),
        "failed": run.get("failed_count"),
        "pass_rate": run.get("test_pass_rate", run.get("score")),
        "tokens": run.get("token_count"),
        "duration_seconds": run.get("run_duration_seconds"),
        "failure_reason": run.get("failure_reason"),
        "submission_id": run.get("submission_id"),
        "requirement_id": run.get("requirement_id"),
        "finished_at": run.get("finished_at"),
    }
    for key in ("display_name", "model_name", "feature_implemented_count", "feature_total_count"):
        if key in run:
            summary[key] = run[key]
    if "token_cost_usd" in run:
        summary["cost"] = {
            "amount": run["token_cost_usd"],
            "currency": run.get("token_cost_currency"),
        }
    if isinstance(run.get("steps"), list):
        summary["steps"] = [
            {key: step.get(key) for key in ("key", "status", "description")}
            for step in run["steps"]
        ]
    if isinstance(run.get("tests"), list):
        summary["failed_tests"] = [
            {key: test.get(key) for key in ("name", "status", "error")}
            for test in run["tests"]
            if test.get("passed") is False
        ]
    if run.get("poll_timed_out"):
        summary["poll_timed_out"] = True
    return summary


def summarize_submission(submission: dict[str, Any]) -> dict[str, Any]:
    return {key: submission[key] for key in SUBMISSION_FIELDS if key in submission}


def log_summary(run_id: str, payload: dict[str, Any], tail: int) -> dict[str, Any]:
    """Return a bounded tail plus the cursor, without duplicating streams."""
    console = payload.get("console") or payload.get("stdout") or payload.get("events") or ""
    if not isinstance(console, str):
        console = json.dumps(console, ensure_ascii=False)
    stderr = payload.get("stderr") or ""
    return {
        "run_id": run_id,
        "console": "\n".join(str(console).splitlines()[-tail:]),
        "stderr": "\n".join(str(stderr).splitlines()[-tail:]),
        "next_offset": payload.get("log_offset"),
        "last_event_id": payload.get("last_event_id"),
    }


def enforce_budget(record_dir: Path, config: SubmitConfig, dry_run: bool = False) -> None:
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


def write_record(path: Path, record: dict[str, Any], secrets: tuple[str, ...] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(sanitize(record, secrets), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
