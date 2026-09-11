"""Capture an ARC-Bench browser session into a local env file."""

from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path


SESSION_COOKIE = "arcbench_session"
STICKY_COOKIE = "arc_api_sticky"
DEFAULT_CHROME_ROOT = Path.home() / "Library/Application Support/Google/Chrome"


def default_env_path() -> Path:
    return Path.cwd() / ".env"


def capture_chrome_cookie(
    profile: str = "Default",
    env_path: Path | None = None,
    chrome_root: Path = DEFAULT_CHROME_ROOT,
) -> tuple[Path, int]:
    """Read the ARC-Bench cookies from Chrome and write them to an env file.

    The implementation follows Chrome's macOS v10 cookie format. It uses a
    temporary copy of the cookie database so Chrome does not need to be closed.
    """
    source = chrome_root / profile / "Cookies"
    if not source.is_file():
        raise RuntimeError(f"Chrome cookie store not found: {source}")

    completed = subprocess.run(
        [
            "security",
            "find-generic-password",
            "-w",
            "-s",
            "Chrome Safe Storage",
            "-a",
            "Chrome",
        ],
        capture_output=True,
        text=True,
    )
    if completed.returncode != 0 or not completed.stdout.strip():
        raise RuntimeError(
            "could not read the Chrome Safe Storage key; approve the keychain prompt and retry"
        )

    key = hashlib.pbkdf2_hmac(
        "sha1",
        completed.stdout.rstrip("\n").encode(),
        b"saltysalt",
        1003,
        16,
    )

    with tempfile.TemporaryDirectory() as tmp:
        copy = Path(tmp) / "Cookies"
        shutil.copy2(source, copy)
        connection = sqlite3.connect(f"file:{copy}?immutable=1", uri=True)
        rows = connection.execute(
            "select name, encrypted_value from cookies "
            "where host_key like '%arc-bench%' and name in (?, ?)",
            (SESSION_COOKIE, STICKY_COOKIE),
        ).fetchall()
        connection.close()

    if not rows:
        raise RuntimeError(
            "no ARC-Bench cookies; sign in at https://arc-bench.com/login first"
        )

    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

    found: dict[str, str] = {}
    for name, encrypted in rows:
        blob = bytes(encrypted)
        if blob[:3] != b"v10":
            raise RuntimeError(f"{name}: unexpected encryption prefix {blob[:3]!r}")
        decryptor = Cipher(algorithms.AES(key), modes.CBC(b" " * 16)).decryptor()
        plain = decryptor.update(blob[3:]) + decryptor.finalize()
        if 1 <= plain[-1] <= 16:
            plain = plain[:-plain[-1]]
        # Chrome 152 prefixes the value with a 32-byte domain-bound hash.
        found[name] = plain[32:].decode("utf-8")

    if SESSION_COOKIE not in found:
        raise RuntimeError(f"{SESSION_COOKIE} missing from the cookie store")

    pairs = [f"{SESSION_COOKIE}={found[SESSION_COOKIE]}"]
    if STICKY_COOKIE in found:
        pairs.append(f"{STICKY_COOKIE}={found[STICKY_COOKIE]}")

    env_path = (env_path or default_env_path()).resolve()
    lines = env_path.read_text(encoding="utf-8").splitlines() if env_path.is_file() else []
    lines = [line for line in lines if not line.startswith("ARC_BENCH_SESSION_COOKIE=")]
    lines.append("ARC_BENCH_SESSION_COOKIE=" + "; ".join(pairs))
    env_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    env_path.chmod(0o600)
    return env_path, len(found[SESSION_COOKIE])
