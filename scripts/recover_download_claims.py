#!/usr/bin/env python3
"""Safely return one abandoned downloader claim to the pending queue.

The command is intentionally narrower than the normal lease recovery in
``Storage``.  It is for an operator who knows the exact downloader identity
that must be recovered.  Dry-run is the default; ``--apply`` is the sole
mutation gate.
"""

from __future__ import annotations

import argparse
import errno
import json
import os
import re
import socket
import sqlite3
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from config import DB_PATH


class RecoveryError(RuntimeError):
    """A bounded, operator-actionable recovery refusal."""


@dataclass(frozen=True)
class RecoveryScope:
    claimed_by: str
    source: str
    artifact_kind: str
    category: str | None = None
    language: str | None = None
    id_min: int | None = None
    id_max: int | None = None
    job_keys: tuple[str, ...] = ()


_WORKER_ID_RE = re.compile(
    r"^(?P<hostname>[^:]+):(?P<pid>[1-9][0-9]*):(?P<nonce>[0-9a-f]{12})$"
)
_REQUIRED_COLUMNS = {
    "id", "source", "artifact_kind", "job_key", "category", "language",
    "status", "claimed_by", "claimed_at", "lease_expires_at",
}
_REPORT_COLUMNS = (
    "id", "source", "artifact_kind", "job_key", "category", "language",
    "status", "claimed_by", "claimed_at", "lease_expires_at",
)


def _nonempty(value: str, label: str) -> str:
    value = str(value).strip()
    if not value:
        raise RecoveryError(f"{label} must not be empty")
    return value


def validate_scope(scope: RecoveryScope) -> RecoveryScope:
    """Validate that every mutating identity component is explicit."""

    _nonempty(scope.claimed_by, "claimed_by")
    _nonempty(scope.source, "source")
    _nonempty(scope.artifact_kind, "artifact_kind")
    if scope.category is not None:
        _nonempty(scope.category, "category")
    if scope.language is not None:
        _nonempty(scope.language, "language")
    if (scope.id_min is None) != (scope.id_max is None):
        raise RecoveryError("id_min and id_max must be provided together")
    if scope.id_min is not None:
        if type(scope.id_min) is not int or type(scope.id_max) is not int:
            raise RecoveryError("id_min and id_max must be integers")
        if scope.id_min <= 0 or scope.id_max <= 0:
            raise RecoveryError("id_min and id_max must be positive")
        if scope.id_min > scope.id_max:
            raise RecoveryError("id_min must not exceed id_max")
    if any(not isinstance(key, str) or not key for key in scope.job_keys):
        raise RecoveryError("job_keys must contain non-empty strings")
    return scope


def load_job_keys(path: Path) -> tuple[str, ...]:
    """Load exact job keys, one UTF-8 value per non-blank, non-comment line."""

    if not path.is_file():
        raise RecoveryError(f"job key file is not a regular file: {path}")
    keys: list[str] = []
    seen: set[str] = set()
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as exc:
        raise RecoveryError(f"cannot read job key file: {type(exc).__name__}") from exc
    for line in lines:
        key = line.strip()
        if not key or key.startswith("#"):
            continue
        if key not in seen:
            seen.add(key)
            keys.append(key)
    if not keys:
        raise RecoveryError("job key file contains no job keys")
    return tuple(keys)


def parse_local_worker_pid(
    claimed_by: str, *, hostname: str | None = None,
) -> int | None:
    """Return a PID only for the current downloader's exact local ID format."""

    match = _WORKER_ID_RE.fullmatch(claimed_by)
    if match is None:
        return None
    if match.group("hostname") != (hostname or socket.gethostname()):
        return None
    return int(match.group("pid"))


def pid_is_alive(pid: int) -> bool:
    """Conservatively treat an inaccessible process as alive."""

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return True
    return True


def _open_database(db_path: Path, *, writable: bool = False) -> sqlite3.Connection:
    resolved = db_path.expanduser().resolve()
    if not resolved.is_file():
        raise RecoveryError(f"database is not a regular file: {resolved}")
    try:
        mode = "rw" if writable else "ro"
        connection = sqlite3.connect(
            f"{resolved.as_uri()}?mode={mode}", uri=True, timeout=60,
        )
    except sqlite3.Error as exc:
        raise RecoveryError(f"cannot open database: {type(exc).__name__}") from exc
    connection.row_factory = sqlite3.Row
    return connection


def _check_schema(connection: sqlite3.Connection) -> None:
    columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(audio_urls)")
    }
    missing = sorted(_REQUIRED_COLUMNS - columns)
    if missing:
        raise RecoveryError(
            "audio_urls is missing required columns: " + ", ".join(missing)
        )


def _where_clause(scope: RecoveryScope) -> tuple[str, list[object]]:
    conditions = [
        "status='downloading'", "claimed_by=?", "source=?", "artifact_kind=?",
    ]
    params: list[object] = [
        scope.claimed_by, scope.source, scope.artifact_kind,
    ]
    if scope.category is not None:
        conditions.append("category=?")
        params.append(scope.category)
    if scope.language is not None:
        conditions.append("language=?")
        params.append(scope.language)
    if scope.id_min is not None:
        conditions.extend(["id>=?", "id<=?"])
        params.extend([scope.id_min, scope.id_max])
    if scope.job_keys:
        placeholders = ",".join("?" for _ in scope.job_keys)
        conditions.append(f"job_key IN ({placeholders})")
        params.extend(scope.job_keys)
    return " AND ".join(conditions), params


def _select_matches(
    connection: sqlite3.Connection, scope: RecoveryScope,
) -> list[dict[str, object]]:
    where, params = _where_clause(scope)
    columns = ", ".join(_REPORT_COLUMNS)
    rows = connection.execute(
        f"SELECT {columns} FROM audio_urls WHERE {where} ORDER BY id", params,
    ).fetchall()
    return [dict(row) for row in rows]


def _next_backup_path(db_path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    stem = f"{db_path.name}.recover-claims-{timestamp}"
    for index in range(1000):
        suffix = "" if index == 0 else f".{index}"
        candidate = db_path.with_name(f"{stem}{suffix}.bak")
        if not candidate.exists():
            return candidate
    raise RecoveryError("cannot allocate a unique backup path")


def backup_database(db_path: Path) -> Path:
    """Create a consistent SQLite backup beside the live database."""

    backup_path = _next_backup_path(db_path)
    source = _open_database(db_path)
    destination: sqlite3.Connection | None = None
    try:
        destination = sqlite3.connect(str(backup_path))
        source.backup(destination)
        result = destination.execute("PRAGMA integrity_check").fetchone()
        if result is None or result[0] != "ok":
            raise RecoveryError("backup database failed integrity_check")
    except BaseException:
        if destination is not None:
            destination.close()
            destination = None
        backup_path.unlink(missing_ok=True)
        raise
    finally:
        source.close()
        if destination is not None:
            destination.close()
    return backup_path


def _scope_report(scope: RecoveryScope) -> dict[str, object]:
    return {
        "claimed_by": scope.claimed_by,
        "source": scope.source,
        "artifact_kind": scope.artifact_kind,
        "category": scope.category,
        "language": scope.language,
        "id_min": scope.id_min,
        "id_max": scope.id_max,
        "job_keys": list(scope.job_keys),
    }


def recover_claims(
    db_path: Path,
    scope: RecoveryScope,
    *,
    apply: bool = False,
    process_probe: Callable[[int], bool] = pid_is_alive,
) -> dict[str, object]:
    """Report or reset only downloading rows owned by one exact worker."""

    scope = validate_scope(scope)
    db_path = db_path.expanduser().resolve()
    connection = _open_database(db_path, writable=apply)
    backup_path: Path | None = None
    try:
        _check_schema(connection)
        if apply:
            connection.execute("BEGIN IMMEDIATE")
        matches = _select_matches(connection, scope)
        local_pid = parse_local_worker_pid(scope.claimed_by)
        local_pid_alive = process_probe(local_pid) if local_pid is not None else None
        if apply and matches and local_pid is None:
            connection.rollback()
            raise RecoveryError(
                "refusing to recover claims whose worker identity cannot be "
                "verified on this host"
            )
        if apply and matches and local_pid_alive:
            connection.rollback()
            raise RecoveryError(
                f"refusing to recover claims from live local worker PID {local_pid}"
            )

        updated_count = 0
        if apply and matches:
            # BEGIN IMMEDIATE above prevents another writer from changing these
            # rows between the backup snapshot, selection and guarded update.
            backup_path = backup_database(db_path)
            where, params = _where_clause(scope)
            ids = [int(row["id"]) for row in matches]
            placeholders = ",".join("?" for _ in ids)
            cursor = connection.execute(
                "UPDATE audio_urls SET status='pending', claimed_by='', "
                "claimed_at='', lease_expires_at='' "
                f"WHERE {where} AND id IN ({placeholders})",
                (*params, *ids),
            )
            updated_count = int(cursor.rowcount)
            if updated_count != len(matches):
                connection.rollback()
                raise RecoveryError(
                    "claim set changed during recovery; no rows were committed"
                )
            connection.commit()
        elif apply:
            connection.rollback()

        return {
            "schema_version": 1,
            "mode": "apply" if apply else "dry-run",
            "status": "applied" if apply and matches else "no-match" if apply else "ready",
            "database": str(db_path),
            "scope": _scope_report(scope),
            "local_worker_pid": local_pid,
            "local_worker_alive": local_pid_alive,
            "matched_count": len(matches),
            "updated_count": updated_count,
            "backup": str(backup_path) if backup_path is not None else None,
            "rows": matches,
        }
    except sqlite3.Error as exc:
        if connection.in_transaction:
            connection.rollback()
        raise RecoveryError(f"SQLite recovery failed: {type(exc).__name__}") from exc
    finally:
        connection.close()


def _positive_int(value: str) -> int:
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Safely recover one exact AudioSpider downloader claim",
    )
    parser.add_argument("--db", type=Path, default=Path(DB_PATH))
    parser.add_argument("--claimed-by", required=True)
    parser.add_argument("--source", required=True)
    parser.add_argument(
        "--artifact-kind", required=True, choices=("audio", "video_bundle"),
    )
    parser.add_argument("--category")
    parser.add_argument("--language")
    parser.add_argument("--id-min", type=_positive_int)
    parser.add_argument("--id-max", type=_positive_int)
    parser.add_argument(
        "--job-keys-file", type=Path,
        help="UTF-8 file containing one exact job_key per line",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="create a SQLite backup and reset the exact matching claims",
    )
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        job_keys = load_job_keys(args.job_keys_file) if args.job_keys_file else ()
        scope = RecoveryScope(
            claimed_by=args.claimed_by,
            source=args.source,
            artifact_kind=args.artifact_kind,
            category=args.category,
            language=args.language,
            id_min=args.id_min,
            id_max=args.id_max,
            job_keys=job_keys,
        )
        report = recover_claims(args.db, scope, apply=args.apply)
    except RecoveryError as exc:
        print(json.dumps({
            "schema_version": 1,
            "mode": "apply" if args.apply else "dry-run",
            "status": "refused",
            "reason": str(exc),
        }, ensure_ascii=False), file=sys.stderr)
        return 2
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
