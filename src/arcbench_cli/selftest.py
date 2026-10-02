"""Direct HTTP client for the organizer's application-only self-test service.

Routes and payloads were verified against the deployed web client on 2026-10-02.
This service has its own login and quota; its results never count on the board.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

from .client import ApiError, OfficialClient, SubmitConfig, sha256_file

BASE_URL = "https://arcbench-selftest-web.vercel.app"
COOKIE_VARIABLE = "ARC_BENCH_SELFTEST_COOKIE"
MAX_ZIP_BYTES = 50 * 1024 * 1024
TERMINAL = {"passed", "failed", "error", "rejected", "not_run", "system_error"}


def validate_app(path: Path) -> dict[str, Any]:
    if not path.is_file() or path.suffix.lower() != ".zip":
        raise ValueError("self-test requires an existing application .zip file")
    size = path.stat().st_size
    if not 0 < size <= MAX_ZIP_BYTES:
        raise ValueError("application ZIP must be nonempty and at most 50 MB")
    try:
        with zipfile.ZipFile(path) as archive:
            names = archive.namelist()
            if "Dockerfile" not in names or not archive.getinfo("Dockerfile").file_size:
                raise ValueError("application ZIP must contain a nonempty root Dockerfile")
            for name in names:
                parts = PurePosixPath(name.replace("\\", "/")).parts
                if name.startswith(("/", "\\")) or ".." in parts or (parts and ":" in parts[0]):
                    raise ValueError("application ZIP contains unsafe archive paths")
                if {".git", "node_modules"} & set(parts):
                    raise ValueError("application ZIP must exclude .git and node_modules")
    except zipfile.BadZipFile as error:
        raise ValueError("application package is not a valid ZIP") from error
    return {"path": str(path), "bytes": size, "sha256": sha256_file(path)}


def _id(value: str) -> str:
    if not value or value in {".", ".."}:
        raise ValueError("a submission/task identifier is required")
    return urllib.parse.quote(value, safe="")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SelftestClient(OfficialClient):
    @classmethod
    def from_env(cls, env: dict[str, str], base_url: str | None = None) -> "SelftestClient":
        # Never send the competition session or model API key to this service.
        return cls(SubmitConfig(
            base_url=(base_url or env.get("ARC_BENCH_SELFTEST_BASE_URL", BASE_URL)).rstrip("/"),
            session_cookie=env.get(COOKIE_VARIABLE, ""),
            timeout_seconds=int(env.get("ARC_BENCH_HTTP_TIMEOUT_SECONDS", "60")),
        ))

    def tasks(self) -> Any:
        return self.request("GET", "/tasks")

    def submissions(self) -> Any:
        return self.request("GET", "/submissions")

    def submission(self, submission_id: str) -> dict[str, Any]:
        payload = self.request("GET", f"/submissions/{_id(submission_id)}")
        if not isinstance(payload, dict) or not isinstance(payload.get("submission"), dict):
            raise ApiError("self-test status response has no submission object")
        record = payload["submission"]
        if record.get("id") != submission_id:
            raise ApiError("self-test status returned a different submission id")
        return record

    def _post(self, path: str, body: dict[str, Any]) -> Any:
        return self.request("POST", path, json.dumps(body).encode(), "application/json")

    def upload_application(self, task: str, package: Path, on_progress=None) -> dict[str, Any]:
        metadata = validate_app(package)
        _id(task)
        allocation = self._post("/upload-url", {"taskId": task, "size": metadata["bytes"]})
        upload_id = allocation.get("id") if isinstance(allocation, dict) else None
        url = allocation.get("uploadUrl") if isinstance(allocation, dict) else None
        if not isinstance(upload_id, str) or not upload_id or not isinstance(url, str):
            raise ApiError("upload allocation is missing id/uploadUrl; inspect self-test history before retrying",
                           uncertain=True, method="POST", path="/upload-url")
        # A signed object-store URL is itself a credential. Keep it out of records.
        self.known_secrets.append(url)
        self.safe(None)
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
            raise ApiError("self-test returned an invalid signed HTTPS upload URL", context={"upload_id": upload_id})
        if on_progress:
            on_progress({"phase": "allocated", "upload_id": upload_id, "package": metadata})
        try:
            # Separate opener: no website cookies or model credentials go to object storage.
            data = package.read_bytes()
            if len(data) != metadata["bytes"] or hashlib.sha256(data).hexdigest() != metadata["sha256"]:
                raise ValueError("application ZIP changed during upload; create a new immutable package")
            request = urllib.request.Request(url, data=data, method="PUT",
                                             headers={"Content-Type": "application/zip"})
            with urllib.request.build_opener(_NoRedirect()).open(
                request, timeout=self.config.timeout_seconds
            ) as response:
                if not 200 <= response.status < 300:
                    raise OSError(f"object upload HTTP {response.status}")
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            raise ApiError("application upload did not complete; no automatic retry",
                           method="PUT", uncertain=True, transport=True,
                           context={"upload_id": upload_id, "exception_type": type(error).__name__}) from None
        try:
            result = self._post("/submit", {"uploadId": upload_id})
        except ApiError as error:
            error.details["upload_id"] = upload_id
            raise
        if not isinstance(result, dict) or not isinstance(result.get("id"), str) or not result["id"]:
            raise ApiError("submission response has no id; inspect self-test history before retrying",
                           uncertain=True, method="POST", path="/submit", context={"upload_id": upload_id})
        return {"record_type": "selftest", "counts_on_leaderboard": False,
                "id": result["id"], "task_id": task, "package": metadata,
                "url": f"{self.config.base_url}/submissions/{_id(result['id'])}"}

    def wait_submission(self, submission_id: str, interval: float = 20,
                        timeout: float = 1800, on_progress=None) -> dict[str, Any]:
        if interval <= 0 or timeout < 0:
            raise ValueError("poll interval must be positive and timeout nonnegative")
        deadline = time.monotonic() + timeout
        while True:
            record = self.submission(submission_id)
            if on_progress:
                on_progress(record)
            status = str(record.get("status", "")).lower()
            if status in TERMINAL:
                return record
            if status not in {"queued", "running"}:
                raise ApiError(f"unknown self-test state: {status!r}; inspect {submission_id}")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return dict(record, poll_timed_out=True)
            time.sleep(min(interval, remaining))

    def screenshot(self, submission_id: str, path: str) -> bytes:
        return self.binary(f"/submissions/{_id(submission_id)}/screenshot?"
                           + urllib.parse.urlencode({"path": path}))


def result_exit(record: dict[str, Any]) -> int:
    if record.get("poll_timed_out"):
        return 2
    status = str(record.get("status", "")).lower()
    return 1 if status in TERMINAL and status != "passed" else 0
