#!/usr/bin/env python3
"""Run one bounded AudioSpider YouTube retry with Edge login state in memory.

This macOS operator helper reads only allow-listed ``youtube.com`` cookies,
decrypts them in this process, and sends a JSON array directly to the remote
``main.py`` standard input. It never creates a cookie file and never places a
cookie value in argv, stdout, stderr, or an environment variable.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
import time
from pathlib import Path


EDGE_USER_DATA = Path.home() / "Library/Application Support/Microsoft Edge"
EDGE_LOCAL_STATE = EDGE_USER_DATA / "Local State"
SAFE_STORAGE_SERVICE = "Microsoft Edge Safe Storage"
SECURITY_BIN = "/usr/bin/security"
SSH_BIN = "/usr/bin/ssh"
REMOTE_HOST = "dev_L4_1gpus"
REMOTE_REPO = "/root/code/github_repos/AudioSpider-fork"
REMOTE_PYTHON = "/root/miniforge3/envs/audiospider/bin/python"
# Keep the loopback port identical on both hosts.  The PO-token provider runs
# locally and receives this proxy URL from remote yt-dlp, so a remapped port
# would point it at the wrong machine-side listener.
REMOTE_YOUTUBE_PROXY = "http://127.0.0.1:18797"
COOKIE_DOMAINS = (".youtube.com", "youtube.com", "www.youtube.com")
COOKIE_ALLOWLIST = (
    "APISID", "HSID", "LOGIN_INFO", "SAPISID", "SID", "SIDCC", "SSID",
    "VISITOR_INFO1_LIVE", "VISITOR_PRIVACY_METADATA", "YSC",
    "__Secure-1PAPISID", "__Secure-1PSID", "__Secure-1PSIDCC",
    "__Secure-1PSIDTS", "__Secure-3PAPISID", "__Secure-3PSID",
    "__Secure-3PSIDCC", "__Secure-3PSIDTS",
)
SAPISID_NAMES = {"SAPISID", "__Secure-1PAPISID", "__Secure-3PAPISID"}
CHROME_EPOCH_OFFSET_SECONDS = 11_644_473_600
CATEGORIES = ("影视", "访谈", "会议论坛")
EDGE_PROFILE_DIRECTORY = re.compile(r"^(?:Default|Profile [1-9][0-9]{0,2})$")


def resolve_cookie_database(profile_directory: str | None = None) -> tuple[str, Path]:
    """Resolve one explicit/current Edge profile without leaving user-data root."""

    if profile_directory is None:
        try:
            state = json.loads(EDGE_LOCAL_STATE.read_text(encoding="utf-8"))
            profile_directory = str((state.get("profile") or {}).get("last_used") or "")
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError("cannot resolve the current Edge profile") from exc
    if not EDGE_PROFILE_DIRECTORY.fullmatch(profile_directory):
        raise ValueError("unsupported Edge profile directory")
    root = EDGE_USER_DATA.resolve()
    database = (root / profile_directory / "Cookies").resolve()
    if root not in database.parents:
        raise ValueError("Edge cookie database escaped the user-data root")
    return profile_directory, database


def _edge_safe_storage_password() -> bytes:
    result = subprocess.run(
        [SECURITY_BIN, "find-generic-password", "-w", "-s", SAFE_STORAGE_SERVICE],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
    )
    password = result.stdout.rstrip(b"\r\n")
    if not password:
        raise RuntimeError("Edge Safe Storage password is empty")
    return password


def _decrypt_aes_cbc(ciphertext: bytes, key: bytes) -> bytes:
    library = ctypes.CDLL("/usr/lib/libSystem.B.dylib")
    crypt = library.CCCrypt
    crypt.argtypes = [
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p,
        ctypes.c_size_t, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_size_t,
        ctypes.c_void_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_size_t),
    ]
    crypt.restype = ctypes.c_int
    source = ctypes.create_string_buffer(ciphertext)
    key_buffer = ctypes.create_string_buffer(key)
    iv_buffer = ctypes.create_string_buffer(b" " * 16)
    output = ctypes.create_string_buffer(len(ciphertext) + 16)
    moved = ctypes.c_size_t()
    status = crypt(
        1, 0, 1, key_buffer, len(key), iv_buffer, source, len(ciphertext),
        output, len(output), ctypes.byref(moved),
    )
    if status != 0:
        raise RuntimeError(f"CommonCrypto failed with status {status}")
    return output.raw[: moved.value]


def _decrypt_cookie(host_key: str, encrypted: bytes, key: bytes, version: int) -> str:
    if not encrypted.startswith((b"v10", b"v11")):
        raise RuntimeError("unsupported Edge cookie encryption version")
    plaintext = _decrypt_aes_cbc(encrypted[3:], key)
    if version >= 24:
        expected = hashlib.sha256(host_key.encode("utf-8")).digest()
        if len(plaintext) < 32 or plaintext[:32] != expected:
            raise RuntimeError("Edge cookie host binding check failed")
        plaintext = plaintext[32:]
    value = plaintext.decode("utf-8")
    if not value or len(value.encode("utf-8")) > 4096 or any(
        ord(character) < 0x20 or ord(character) == 0x7F for character in value
    ):
        raise RuntimeError("invalid Edge cookie value")
    return value


def load_youtube_cookies(profile_directory: str | None = None) -> list[dict]:
    """Load the minimum domain-scoped Edge fields without exposing values."""

    _, cookie_db = resolve_cookie_database(profile_directory)
    if not cookie_db.is_file():
        raise FileNotFoundError("Edge cookie database does not exist")
    password = _edge_safe_storage_password()
    key = hashlib.pbkdf2_hmac("sha1", password, b"saltysalt", 1003, dklen=16)
    connection = sqlite3.connect(f"{cookie_db.as_uri()}?mode=ro", uri=True)
    try:
        version_row = connection.execute(
            "SELECT value FROM meta WHERE key='version'"
        ).fetchone()
        version = int(version_row[0]) if version_row else 0
        domain_slots = ",".join("?" for _ in COOKIE_DOMAINS)
        name_slots = ",".join("?" for _ in COOKIE_ALLOWLIST)
        rows = connection.execute(
            "SELECT host_key, name, encrypted_value, expires_utc, path, is_secure "
            f"FROM cookies WHERE host_key IN ({domain_slots}) "
            f"AND name IN ({name_slots}) "
            "ORDER BY host_key, name, length(path) DESC, expires_utc DESC",
            COOKIE_DOMAINS + COOKIE_ALLOWLIST,
        ).fetchall()
    finally:
        connection.close()

    now = time.time()
    cookies: list[dict] = []
    seen: set[tuple[str, str, str]] = set()
    for host_key, name, encrypted, expires_utc, path, is_secure in rows:
        if not is_secure or not isinstance(path, str) or not path.startswith("/"):
            continue
        identity = (name, host_key, path)
        if identity in seen:
            continue
        expires_at = (
            float(expires_utc) / 1_000_000 - CHROME_EPOCH_OFFSET_SECONDS
            if expires_utc else None
        )
        if expires_at is not None and expires_at <= now:
            continue
        value = _decrypt_cookie(host_key, bytes(encrypted), key, version)
        seen.add(identity)
        cookies.append({
            "name": name,
            "value": value,
            "domain": host_key,
            "path": path,
            "secure": True,
            "expires": int(expires_at) if expires_at is not None else None,
        })
    names = {cookie["name"] for cookie in cookies}
    if "LOGIN_INFO" not in names or not names.intersection(SAPISID_NAMES):
        raise RuntimeError("required authenticated YouTube cookies are missing or expired")
    encoded = json.dumps(cookies, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > 64 * 1024:
        raise RuntimeError("YouTube cookie payload is too large")
    return cookies


def remote_command(*, batch_id: str, category: str, limit: int,
                   retry_failed: bool = True) -> str:
    if not batch_id or len(batch_id) > 80 or not all(
        character.isalnum() or character in "._-" for character in batch_id
    ):
        raise ValueError("invalid batch id")
    if category not in CATEGORIES or not 1 <= limit <= 50:
        raise ValueError("category or limit is outside the operator safety bound")
    retry_flag = "--retry-failed " if retry_failed else ""
    return (
        f"cd {REMOTE_REPO} && "
        "env -u ALL_PROXY -u all_proxy "
        f"HTTP_PROXY={REMOTE_YOUTUBE_PROXY} HTTPS_PROXY={REMOTE_YOUTUBE_PROXY} "
        f"http_proxy={REMOTE_YOUTUBE_PROXY} https_proxy={REMOTE_YOUTUBE_PROXY} "
        "NO_PROXY=127.0.0.1,localhost no_proxy=127.0.0.1,localhost "
        f"{REMOTE_PYTHON} main.py {retry_flag}--batch-id {batch_id} "
        f"--source youtube --category {category} --language en "
        f"--artifact-kind video_bundle --limit {limit} --workers 1 "
        "--format original --allow-youtube-cookie"
    )


def _run_remote_with_payload(command: str, payload: bytes) -> int:
    process = subprocess.Popen(
        [
            SSH_BIN, "-o", "BatchMode=yes", "-o", "NumberOfPasswordPrompts=0",
            "-o", "ControlMaster=no", "-o", "ControlPath=none",
            "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=30",
            "-o", "ServerAliveCountMax=3", REMOTE_HOST, command,
        ],
        stdin=subprocess.PIPE,
    )
    assert process.stdin is not None
    try:
        process.stdin.write(payload)
        process.stdin.flush()
    finally:
        process.stdin.close()
    return process.wait()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--batch-id", required=True)
    parser.add_argument("--category", choices=CATEGORIES, default="影视")
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument(
        "--edge-profile", default=None,
        help="Edge profile directory (Default/Profile N); default uses Local State last_used",
    )
    parser.add_argument(
        "--pending", action="store_true",
        help="consume pending rows instead of retrying failed rows",
    )
    parser.add_argument(
        "--audit-only", action="store_true",
        help="skip download and only run the in-memory persistence leak audit",
    )
    args = parser.parse_args()
    command = remote_command(
        batch_id=args.batch_id, category=args.category, limit=args.limit,
        retry_failed=not args.pending,
    )
    profile_directory, _ = resolve_cookie_database(args.edge_profile)
    payload = json.dumps(
        load_youtube_cookies(profile_directory), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    download_status = 0
    if not args.audit_only:
        print(
            f"YouTube 登录态已从 Edge {profile_directory} 读入本机内存，"
            "正在启动受限远程任务。",
            file=sys.stderr,
        )
        try:
            download_status = _run_remote_with_payload(command, payload)
        except (OSError, BrokenPipeError, subprocess.SubprocessError) as exc:
            download_status = 4
            print(
                f"受限远程任务启动/传输失败：{type(exc).__name__}；继续执行泄漏审计。",
                file=sys.stderr,
            )
    print("YouTube Cookie 泄漏审计开始（仅输出命中数）。", file=sys.stderr)
    try:
        audit_status = _run_remote_with_payload(
            f"cd {REMOTE_REPO} && {REMOTE_PYTHON} "
            "scripts/audit_youtube_cookie_leaks.py",
            payload,
        )
    except (OSError, BrokenPipeError, subprocess.SubprocessError) as exc:
        audit_status = 5
        print(f"泄漏审计无法执行：{type(exc).__name__}", file=sys.stderr)
    return download_status or audit_status


if __name__ == "__main__":
    raise SystemExit(main())
