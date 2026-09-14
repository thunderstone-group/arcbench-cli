"""Capture an ARC-Bench browser session into a local env file.

This is the only part of the CLI that touches the browser, and it runs once per
login. Everything else is plain HTTP against the platform API. The reader is
macOS Chrome specific; on other systems set ``ARC_BENCH_SESSION_COOKIE`` by
hand, as the README describes.
"""

from __future__ import annotations

import hashlib
import platform
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

from .aes import decrypt_cbc, strip_pkcs7

SESSION_COOKIE = "arcbench_session"
STICKY_COOKIE = "arc_api_sticky"
DEFAULT_CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"
DOMAIN_HASH_LENGTH = 32
SAFE_STORAGE_ITERATIONS = 1003
COOKIE_IV = b" " * 16


def default_env_path() -> Path:
    return Path.cwd() / ".env"


def _is_printable(raw: bytes) -> bool:
    return bool(raw) and all(0x20 <= byte < 0x7F for byte in raw)


def cookie_plaintext(plain: bytes) -> str:
    """Return the cookie value, tolerating both Chrome plaintext layouts.

    Chrome 152 prefixes the decrypted value with a 32-byte domain-bound hash.
    Those 32 bytes are effectively random, so a printable prefix identifies the
    older layout where the plaintext is the value itself.
    """
    if len(plain) > DOMAIN_HASH_LENGTH and not _is_printable(plain[:DOMAIN_HASH_LENGTH]):
        plain = plain[DOMAIN_HASH_LENGTH:]
    try:
        return plain.decode("utf-8")
    except UnicodeDecodeError as error:
        raise RuntimeError("decrypted cookie is not valid UTF-8; is the Chrome profile correct?") from error


def _safe_storage_key() -> bytes:
    completed = subprocess.run(
        ["security", "find-generic-password", "-w", "-s", "Chrome Safe Storage", "-a", "Chrome"],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(
            "could not read the Chrome Safe Storage key; approve the keychain prompt and retry"
        )
    return hashlib.pbkdf2_hmac(
        "sha1", completed.stdout.rstrip("\n").encode(), b"saltysalt", SAFE_STORAGE_ITERATIONS, 16
    )


def read_chrome_cookies(profile: str = "Default", chrome_root: Path = DEFAULT_CHROME_ROOT) -> dict[str, str]:
    """Return the ARC-Bench cookies held by a local Chrome profile."""
    if platform.system() != "Darwin":
        raise RuntimeError(
            "`arcbench session` reads macOS Chrome only; on this system export "
            "ARC_BENCH_SESSION_COOKIE manually (see the README)"
        )
    source = chrome_root / profile / "Cookies"
    if not source.is_file():
        raise RuntimeError(f"Chrome cookie store not found: {source}")

    key = _safe_storage_key()
    with tempfile.TemporaryDirectory() as tmp:
        # Copy first so Chrome does not have to be closed.
        copy = Path(tmp) / "Cookies"
        shutil.copy2(source, copy)
        connection = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
        try:
            rows = connection.execute(
                "select name, encrypted_value from cookies "
                "where host_key like '%arc-bench%' and name in (?, ?)",
                (SESSION_COOKIE, STICKY_COOKIE),
            ).fetchall()
        finally:
            connection.close()

    if not rows:
        raise RuntimeError("no ARC-Bench cookies; sign in at https://arc-bench.com/login first")

    found: dict[str, str] = {}
    for name, encrypted in rows:
        blob = bytes(encrypted)
        if blob[:3] != b"v10":
            raise RuntimeError(f"{name}: unexpected encryption prefix {blob[:3]!r}")
        found[name] = cookie_plaintext(strip_pkcs7(decrypt_cbc(blob[3:], key, COOKIE_IV)))
    if SESSION_COOKIE not in found:
        raise RuntimeError(f"{SESSION_COOKIE} missing from the cookie store")
    return found


def format_cookie_header(cookies: dict[str, str]) -> str:
    pairs = [f"{SESSION_COOKIE}={cookies[SESSION_COOKIE]}"]
    if STICKY_COOKIE in cookies:
        pairs.append(f"{STICKY_COOKIE}={cookies[STICKY_COOKIE]}")
    return "; ".join(pairs)


def write_env_cookie(env_path: Path, header: str, variable: str = "ARC_BENCH_SESSION_COOKIE") -> Path:
    """Replace one variable in an env file, leaving the other lines intact."""
    env_path = env_path.resolve()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    lines = [line for line in lines if not line.startswith(variable + "=")]
    lines.append(f"{variable}={header}")
    env_path.parent.mkdir(parents=True, exist_ok=True)
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return env_path


def capture_chrome_cookie(
    profile: str = "Default",
    env_path: Path | None = None,
    chrome_root: Path = DEFAULT_CHROME_ROOT,
) -> tuple[Path, int]:
    """Read the ARC-Bench cookies from Chrome and write them to an env file."""
    cookies = read_chrome_cookies(profile, chrome_root)
    written = write_env_cookie(env_path or default_env_path(), format_cookie_header(cookies))
    return written, len(cookies[SESSION_COOKIE])
