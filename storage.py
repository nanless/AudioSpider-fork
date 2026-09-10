# Copyright (c) 2026 Hao Yin. All rights reserved.

"""SQLite 存储：URL 去重、下载状态追踪、增量爬取标记、内容指纹"""

import hashlib
import os
import re
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from config import DB_PATH, DOWNLOAD_DIR


@dataclass
class AudioRecord:
    url: str
    source: str
    title: str = ""
    file_format: str = ""
    file_size: int = 0
    duration: int = 0
    language: str = ""
    category: str = ""
    speaker: str = ""
    webpage_url: str = ""
    description: str = ""
    author: str = ""
    cover_url: str = ""
    metadata_json: str = ""
    status: str = "pending"
    local_path: str = ""
    content_hash: str = ""
    source_id: str = ""
    published_at: str = ""
    discovered_at: str = ""
    downloaded_at: str = ""
    artifact_kind: str = "audio"
    bundle_path: str = ""
    job_key: str = ""


class Storage:
    def __init__(self, db_path: str = DB_PATH):
        self.db_path = db_path
        # Connection caches must belong to the Storage instance.  A class-level
        # thread local silently reused the first database for later instances.
        self._local = threading.local()
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        if not hasattr(self._local, "conn") or self._local.conn is None:
            self._local.conn = sqlite3.connect(self.db_path, timeout=60)
            self._local.conn.row_factory = sqlite3.Row
            self._local.conn.execute("PRAGMA temp_store = MEMORY")
            self._local.conn.execute("PRAGMA journal_mode = WAL")
        return self._local.conn

    def _init_db(self):
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._create_audio_urls_table(conn)
            conn.execute("""
                CREATE TABLE IF NOT EXISTS crawl_checkpoints (
                    source TEXT NOT NULL,
                    checkpoint_key TEXT NOT NULL,
                    checkpoint_value TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (source, checkpoint_key)
                )
            """)
            # Additive preparation gives every supported legacy layout the full
            # canonical column set before a possible table rebuild.
            self._add_column_if_missing(conn, "audio_urls", "published_at", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "claimed_by", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "claimed_at", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "lease_expires_at", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "webpage_url", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "description", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "author", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "cover_url", "TEXT DEFAULT ''")
            self._add_column_if_missing(conn, "audio_urls", "metadata_json", "TEXT DEFAULT ''")
            self._add_column_if_missing(
                conn, "audio_urls", "artifact_kind", "TEXT NOT NULL DEFAULT 'audio'",
            )
            self._add_column_if_missing(conn, "audio_urls", "bundle_path", "TEXT DEFAULT ''")
            self._add_column_if_missing(
                conn, "audio_urls", "job_key", "TEXT NOT NULL DEFAULT ''",
            )
            conn.execute(
                "UPDATE audio_urls SET artifact_kind='audio' "
                "WHERE artifact_kind IS NULL OR artifact_kind=''"
            )
            conn.execute(
                "UPDATE audio_urls SET job_key='' WHERE job_key IS NULL"
            )
            if self._has_global_unique_url(conn):
                self._rebuild_audio_urls_table(conn)
            self._create_audio_urls_indexes(conn)
            # Recover only legacy rows that predate leases. Active leased work is
            # recovered by claim methods after the lease expires.
            conn.execute(
                "UPDATE audio_urls SET status='pending' "
                "WHERE status='downloading' AND COALESCE(lease_expires_at, '')=''"
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    @staticmethod
    def _create_audio_urls_table(conn: sqlite3.Connection):
        conn.execute("""
            CREATE TABLE IF NOT EXISTS audio_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT NOT NULL,
                source TEXT NOT NULL,
                artifact_kind TEXT NOT NULL DEFAULT 'audio',
                job_key TEXT NOT NULL DEFAULT '',
                title TEXT DEFAULT '',
                file_format TEXT DEFAULT '',
                file_size INTEGER DEFAULT 0,
                duration INTEGER DEFAULT 0,
                language TEXT DEFAULT '',
                category TEXT DEFAULT '',
                speaker TEXT DEFAULT '',
                webpage_url TEXT DEFAULT '',
                description TEXT DEFAULT '',
                author TEXT DEFAULT '',
                cover_url TEXT DEFAULT '',
                metadata_json TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                local_path TEXT DEFAULT '',
                bundle_path TEXT DEFAULT '',
                content_hash TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                published_at TEXT DEFAULT '',
                discovered_at TEXT NOT NULL,
                downloaded_at TEXT DEFAULT '',
                claimed_by TEXT DEFAULT '',
                claimed_at TEXT DEFAULT '',
                lease_expires_at TEXT DEFAULT ''
            )
        """)

    @staticmethod
    def _has_global_unique_url(conn: sqlite3.Connection) -> bool:
        for index in conn.execute("PRAGMA index_list(audio_urls)"):
            if not index["unique"] or index["partial"]:
                continue
            quoted_name = index["name"].replace('"', '""')
            columns = [
                row["name"]
                for row in conn.execute(f'PRAGMA index_info("{quoted_name}")')
            ]
            if columns == ["url"]:
                return True
        return False

    @classmethod
    def _rebuild_audio_urls_table(cls, conn: sqlite3.Connection):
        legacy_columns = list(conn.execute("PRAGMA table_info(audio_urls)"))
        columns = [row["name"] for row in legacy_columns]
        conn.execute("ALTER TABLE audio_urls RENAME TO audio_urls_legacy_unique_url")
        cls._create_audio_urls_table(conn)
        canonical_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(audio_urls)")
        }
        for column in legacy_columns:
            if column["name"] in canonical_columns:
                continue
            quoted_name = column["name"].replace('"', '""')
            definition = f'"{quoted_name}" {column["type"] or "TEXT"}'
            if column["notnull"]:
                definition += " NOT NULL"
            if column["dflt_value"] is not None:
                definition += f' DEFAULT {column["dflt_value"]}'
            conn.execute(f"ALTER TABLE audio_urls ADD COLUMN {definition}")
        quoted_columns = ", ".join(
            f'"{name.replace(chr(34), chr(34) * 2)}"' for name in columns
        )
        conn.execute(
            f"INSERT INTO audio_urls ({quoted_columns}) "
            f"SELECT {quoted_columns} FROM audio_urls_legacy_unique_url"
        )
        conn.execute("DROP TABLE audio_urls_legacy_unique_url")

    @staticmethod
    def _create_audio_urls_indexes(conn: sqlite3.Connection):
        statements = (
            "CREATE INDEX IF NOT EXISTS idx_status ON audio_urls(status)",
            "CREATE INDEX IF NOT EXISTS idx_source ON audio_urls(source)",
            "CREATE INDEX IF NOT EXISTS idx_source_id ON audio_urls(source, source_id)",
            "CREATE INDEX IF NOT EXISTS idx_content_hash ON audio_urls(content_hash)",
            "CREATE INDEX IF NOT EXISTS idx_category_status ON audio_urls(category, status)",
            "CREATE INDEX IF NOT EXISTS idx_language_status ON audio_urls(language, status)",
            "CREATE INDEX IF NOT EXISTS idx_published_status ON audio_urls(published_at, status)",
            "CREATE INDEX IF NOT EXISTS idx_lease_expires_at "
            "ON audio_urls(status, lease_expires_at)",
            "CREATE INDEX IF NOT EXISTS idx_source_artifact_id "
            "ON audio_urls(source, source_id, artifact_kind)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_legacy_url "
            "ON audio_urls(url) WHERE job_key=''",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_artifact_job "
            "ON audio_urls(source, artifact_kind, job_key) WHERE job_key!=''",
        )
        for statement in statements:
            conn.execute(statement)

    @staticmethod
    def _add_column_if_missing(conn: sqlite3.Connection, table: str,
                               column: str, definition: str):
        columns = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # ── 增量爬取标记 ──

    def get_checkpoint(self, source: str, key: str) -> str | None:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT checkpoint_value FROM crawl_checkpoints WHERE source=? AND checkpoint_key=?",
            (source, key),
        ).fetchone()
        return row["checkpoint_value"] if row else None

    def set_checkpoint(self, source: str, key: str, value: str):
        conn = self._get_conn()
        conn.execute(
            "INSERT OR REPLACE INTO crawl_checkpoints "
            "(source, checkpoint_key, checkpoint_value, updated_at) VALUES (?, ?, ?, ?)",
            (source, key, value, datetime.now().isoformat()),
        )
        conn.commit()

    # ── URL 管理 ──

    def add_url(self, record: AudioRecord) -> bool:
        conn = self._get_conn()
        try:
            record.artifact_kind = record.artifact_kind or "audio"
            record.job_key = record.job_key or ""
            if record.job_key:
                if self.job_key_exists(
                    record.source, record.artifact_kind, record.job_key,
                ):
                    return False
            elif ((record.source_id and self.source_id_exists(
                    record.source, record.source_id, record.artifact_kind,
                  )) or self.url_exists(record.url)):
                return False
            record.discovered_at = record.discovered_at or datetime.now().isoformat()
            cursor = conn.execute(
                "INSERT OR IGNORE INTO audio_urls "
                "(url, source, artifact_kind, job_key, title, file_format, file_size, duration, language, "
                "category, speaker, webpage_url, description, author, cover_url, "
                "metadata_json, status, local_path, bundle_path, content_hash, source_id, "
                "published_at, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.url, record.source, record.artifact_kind, record.job_key,
                 record.title, record.file_format,
                 record.file_size, record.duration, record.language,
                 record.category, record.speaker, record.webpage_url,
                 record.description, record.author, record.cover_url,
                 record.metadata_json, record.status, record.local_path,
                 record.bundle_path, record.content_hash, record.source_id,
                 record.published_at, record.discovered_at),
            )
            conn.commit()
            return cursor.rowcount == 1
        except sqlite3.Error:
            return False

    def add_urls_batch(self, records: list[AudioRecord]) -> tuple[int, int]:
        """批量入库。返回 (新增条数, 更新旧记录数)。"""
        conn = self._get_conn()
        now = datetime.now().isoformat()
        legacy_urls = [record.url for record in records if not record.job_key]
        existing_urls = set()
        for offset in range(0, len(legacy_urls), 500):
            chunk = legacy_urls[offset:offset + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            existing_urls.update(
                row["url"] for row in conn.execute(
                    f"SELECT url FROM audio_urls WHERE job_key='' "
                    f"AND url IN ({placeholders})", chunk,
                )
            )
        existing_source_ids = set()
        existing_job_keys = set()
        for record in records:
            record.artifact_kind = record.artifact_kind or "audio"
            record.job_key = record.job_key or ""
            if record.job_key:
                row = conn.execute(
                    "SELECT source, artifact_kind, job_key FROM audio_urls "
                    "WHERE source=? AND artifact_kind=? AND job_key=? LIMIT 1",
                    (record.source, record.artifact_kind, record.job_key),
                ).fetchone()
                if row:
                    existing_job_keys.add(
                        (row["source"], row["artifact_kind"], row["job_key"])
                    )
                continue
            if not record.source_id:
                continue
            row = conn.execute(
                "SELECT source, source_id, artifact_kind FROM audio_urls "
                "WHERE source=? AND source_id=? AND artifact_kind=? "
                "AND job_key='' LIMIT 1",
                (record.source, record.source_id, record.artifact_kind),
            ).fetchone()
            if row:
                existing_source_ids.add(
                    (row["source"], row["source_id"], row["artifact_kind"])
                )
        existing_records = []
        new_records = []
        seen_source_ids = set(existing_source_ids)
        seen_job_keys = set(existing_job_keys)
        seen_urls = set(existing_urls)
        for record in records:
            job_identity = (
                record.source, record.artifact_kind, record.job_key,
            ) if record.job_key else None
            source_identity = (
                record.source, record.source_id, record.artifact_kind,
            ) if not record.job_key and record.source_id else None
            duplicate = (
                job_identity in seen_job_keys if job_identity is not None
                else record.url in seen_urls or source_identity in seen_source_ids
            )
            if duplicate:
                existing_records.append(record)
                continue
            new_records.append(record)
            if job_identity is not None:
                seen_job_keys.add(job_identity)
            else:
                seen_urls.add(record.url)
                if source_identity is not None:
                    seen_source_ids.add(source_identity)
        rows = [
            (r.url, r.source, r.artifact_kind, r.job_key, r.title, r.file_format, r.file_size,
             r.duration, r.language, r.category, r.speaker,
             r.webpage_url, r.description, r.author, r.cover_url, r.metadata_json,
             r.status, r.local_path, r.bundle_path, r.content_hash,
             r.source_id, r.published_at or "", r.discovered_at or now)
            for r in new_records
        ]
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO audio_urls "
            "(url, source, artifact_kind, job_key, title, file_format, file_size, duration, language, "
            "category, speaker, webpage_url, description, author, cover_url, "
            "metadata_json, status, local_path, bundle_path, content_hash, source_id, "
            "published_at, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        added = conn.total_changes - before
        updated = self._enrich_records(
            conn, existing_records,
        )
        conn.commit()
        return added, updated

    @staticmethod
    def _enrich_records(conn: sqlite3.Connection,
                        records: list[AudioRecord]) -> int:
        updated = 0
        for record in records:
            record.artifact_kind = record.artifact_kind or "audio"
            record.job_key = record.job_key or ""
            values = (
                record.title, record.title,
                record.file_size, record.file_size,
                record.duration, record.duration,
                record.language, record.language,
                record.category, record.category,
                record.speaker, record.speaker,
                record.webpage_url, record.webpage_url,
                record.description, record.description,
                record.author, record.author,
                record.cover_url, record.cover_url,
                record.metadata_json, record.metadata_json,
                record.bundle_path, record.bundle_path,
                record.source_id, record.source_id,
                record.published_at, record.published_at,
                record.job_key, record.source, record.artifact_kind,
                record.job_key,
                record.job_key, record.source_id, record.source,
                record.source_id, record.artifact_kind, record.url,
            )
            cursor = conn.execute(
                "UPDATE audio_urls SET "
                "title=CASE WHEN ?!='' THEN ? ELSE title END, "
                "file_size=CASE WHEN ?>0 THEN ? ELSE file_size END, "
                "duration=CASE WHEN ?>0 THEN ? ELSE duration END, "
                "language=CASE WHEN ?!='' THEN ? ELSE language END, "
                "category=CASE WHEN ?!='' THEN ? ELSE category END, "
                "speaker=CASE WHEN ?!='' THEN ? ELSE speaker END, "
                "webpage_url=CASE WHEN ?!='' THEN ? ELSE webpage_url END, "
                "description=CASE WHEN ?!='' THEN ? ELSE description END, "
                "author=CASE WHEN ?!='' THEN ? ELSE author END, "
                "cover_url=CASE WHEN ?!='' THEN ? ELSE cover_url END, "
                "metadata_json=CASE WHEN ?!='' THEN ? ELSE metadata_json END, "
                "bundle_path=CASE WHEN ?!='' THEN ? ELSE bundle_path END, "
                "source_id=CASE WHEN ?!='' THEN ? ELSE source_id END, "
                "published_at=CASE WHEN ?!='' THEN ? ELSE published_at END "
                "WHERE (?!='' AND source=? AND artifact_kind=? AND job_key=?) "
                "OR (?='' AND job_key='' AND "
                "((?!='' AND source=? AND source_id=? AND artifact_kind=?) OR url=?))",
                values,
            )
            updated += cursor.rowcount
        return updated

    def enrich_records(self, records: list[AudioRecord]) -> int:
        if not records:
            return 0
        conn = self._get_conn()
        updated = self._enrich_records(conn, records)
        conn.commit()
        return updated

    def backfill_published(self, records: list[AudioRecord]) -> int:
        values = [(record.published_at, record.url)
                  for record in records if record.published_at]
        if not values:
            return 0
        conn = self._get_conn()
        before = conn.total_changes
        conn.executemany(
            "UPDATE audio_urls SET published_at=? "
            "WHERE url=? AND (published_at='' OR published_at IS NULL)",
            values,
        )
        changed = conn.total_changes - before
        conn.commit()
        return changed

    def get_pending(
        self,
        limit: int = 50,
        source: str | None = None,
        category: str | None = None,
        language: str | None = None,
        per_source: bool = False,
        per_category: bool = False,
        published_since: str | None = None,
        published_before: str | None = None,
        artifact_kind: str | None = None,
    ) -> list[dict]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if per_source and per_category:
            raise ValueError("per_source and per_category are mutually exclusive")
        group_col = "source" if per_source else "category" if per_category else None
        return self._select_records(
            self._get_conn(), "pending", limit, source, category, language,
            group_col, published_since, published_before, artifact_kind,
        )

    @staticmethod
    def _filters(status: str, source: str | None, category: str | None,
                 language: str | None, published_since: str | None,
                 published_before: str | None,
                 artifact_kind: str | None = None) -> tuple[list[str], list]:
        conditions = ["status=?"]
        params: list = [status]
        if source:
            conditions.append("source=?")
            params.append(source)
        if category:
            conditions.append("category=?")
            params.append(category)
        if language:
            conditions.append("language=?")
            params.append(language)
        if artifact_kind:
            conditions.append("artifact_kind=?")
            params.append(artifact_kind)
        if published_since:
            conditions.append("published_at >= ?")
            params.append(published_since)
        if published_before:
            if re.fullmatch(r"\d{4}-\d{2}-\d{2}", published_before):
                next_day = datetime.fromisoformat(published_before) + timedelta(days=1)
                conditions.append("published_at < ?")
                params.append(next_day.date().isoformat())
            else:
                conditions.append("published_at <= ?")
                params.append(published_before)
        return conditions, params

    def _select_records(self, conn: sqlite3.Connection, status: str, limit: int,
                        source: str | None = None, category: str | None = None,
                        language: str | None = None, group_col: str | None = None,
                        published_since: str | None = None,
                        published_before: str | None = None,
                        artifact_kind: str | None = None) -> list[dict]:
        conditions, params = self._filters(
            status, source, category, language, published_since, published_before,
            artifact_kind,
        )
        where = " AND ".join(conditions)
        if group_col:
            if group_col not in {"source", "category"}:
                raise ValueError("unsupported grouping column")
            rows = conn.execute(
                f"SELECT * FROM ("
                f"SELECT audio_urls.*, ROW_NUMBER() OVER "
                f"(PARTITION BY {group_col} ORDER BY id) AS _group_row "
                f"FROM audio_urls WHERE {where} AND {group_col}!=''"
                f") WHERE _group_row<=? ORDER BY id",
                (*params, limit),
            ).fetchall()
            results = []
            for row in rows:
                item = dict(row)
                item.pop("_group_row", None)
                results.append(item)
            return results
        rows = conn.execute(
            f"SELECT * FROM audio_urls WHERE {where} ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _utc_now() -> datetime:
        return datetime.now(timezone.utc)

    def _claim(self, status: str, limit: int, worker_id: str,
               lease_seconds: int, source: str | None = None,
               category: str | None = None, language: str | None = None,
               group_col: str | None = None,
               published_since: str | None = None,
               published_before: str | None = None,
               artifact_kind: str | None = None) -> list[dict]:
        if limit <= 0 or lease_seconds <= 0:
            raise ValueError("limit and lease_seconds must be positive")
        conn = self._get_conn()
        now = self._utc_now()
        now_text = now.isoformat()
        lease_text = (now + timedelta(seconds=lease_seconds)).isoformat()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                "UPDATE audio_urls SET status='pending', claimed_by='', claimed_at='', "
                "lease_expires_at='' WHERE status='downloading' "
                "AND lease_expires_at!='' AND lease_expires_at<=?",
                (now_text,),
            )
            records = self._select_records(
                conn, status, limit, source, category, language, group_col,
                published_since, published_before, artifact_kind,
            )
            ids = [record["id"] for record in records]
            if ids:
                placeholders = ",".join("?" for _ in ids)
                conn.execute(
                    f"UPDATE audio_urls SET status='downloading', claimed_by=?, "
                    f"claimed_at=?, lease_expires_at=? WHERE status=? "
                    f"AND id IN ({placeholders})",
                    (worker_id, now_text, lease_text, status, *ids),
                )
                for record in records:
                    record.update(
                        status="downloading", claimed_by=worker_id,
                        claimed_at=now_text, lease_expires_at=lease_text,
                    )
            conn.commit()
            return records
        except Exception:
            conn.rollback()
            raise

    def claim_pending(self, limit: int, worker_id: str, lease_seconds: int,
                      source: str | None = None, category: str | None = None,
                      language: str | None = None, per_source: bool = False,
                      per_category: bool = False,
                      published_since: str | None = None,
                      published_before: str | None = None,
                      artifact_kind: str | None = None) -> list[dict]:
        if per_source and per_category:
            raise ValueError("per_source and per_category are mutually exclusive")
        group_col = "source" if per_source else "category" if per_category else None
        return self._claim(
            "pending", limit, worker_id, lease_seconds, source, category,
            language, group_col, published_since, published_before, artifact_kind,
        )

    def claim_failed(self, limit: int, worker_id: str, lease_seconds: int,
                     source: str | None = None,
                     artifact_kind: str | None = None) -> list[dict]:
        return self._claim(
            "failed", limit, worker_id, lease_seconds, source=source,
            artifact_kind=artifact_kind,
        )

    def update_status(self, url: str, status: str, local_path: str = "",
                      job_key: str | None = None):
        """Update one job; omitted ``job_key`` targets the legacy jobless row."""
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            if status == "done":
                cursor = conn.execute(
                    "UPDATE audio_urls SET status=?, local_path=?, downloaded_at=?, "
                    "claimed_by='', claimed_at='', lease_expires_at='' "
                    "WHERE url=? AND job_key=?",
                    (status, local_path, datetime.now().isoformat(), url, job_key or ""),
                )
            else:
                cursor = conn.execute(
                    "UPDATE audio_urls SET status=?, claimed_by='', claimed_at='', "
                    "lease_expires_at='' WHERE url=? AND job_key=?",
                    (status, url, job_key or ""),
                )
            if job_key and cursor.rowcount != 1:
                raise ValueError("job record was not found or its key was ambiguous")
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def finalize_download(self, url: str, local_path: str, file_format: str,
                          file_size: int, content_hash: str) -> bool:
        """Atomically finalize a download. Return True when it is a duplicate."""
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            target = conn.execute(
                "SELECT artifact_kind FROM audio_urls WHERE url=? AND job_key=''",
                (url,),
            ).fetchone()
            duplicate = None
            if target is not None and target["artifact_kind"] == "audio":
                duplicate = conn.execute(
                    "SELECT 1 FROM audio_urls WHERE content_hash=? AND status='done' "
                    "AND artifact_kind='audio' AND url!=? LIMIT 1",
                    (content_hash, url),
                ).fetchone()
            stored_path = f"dup:{content_hash}" if duplicate else local_path
            conn.execute(
                "UPDATE audio_urls SET status='done', local_path=?, file_format=?, "
                "file_size=?, content_hash=?, downloaded_at=?, claimed_by='', "
                "claimed_at='', lease_expires_at='' WHERE url=? AND job_key=''",
                (stored_path, file_format, file_size, content_hash,
                 datetime.now().isoformat(), url),
            )
            conn.commit()
            return duplicate is not None
        except Exception:
            conn.rollback()
            raise

    def finalize_artifact(self, url: str, local_path: str, bundle_path: str,
                          file_format: str, file_size: int,
                          content_hash: str,
                          artifact_kind: str | None = None,
                          job_key: str | None = None) -> bool:
        """Finalize a bundle-like artifact without content-based deduplication.

        Bundle hashes describe audit closure; equal hashes do not authorize callers
        to delete either bundle's primary file.  The return value is therefore
        always ``False`` and mirrors ``finalize_download`` for simple callers.
        """
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            artifact_assignment = "" if artifact_kind is None else ", artifact_kind=?"
            artifact_filter = "" if artifact_kind is None else " AND artifact_kind=?"
            params: tuple = (
                local_path, bundle_path, file_format, file_size, content_hash,
                datetime.now().isoformat(),
            )
            if artifact_kind is not None:
                params = (*params, artifact_kind)
            params = (*params, url, job_key or "")
            if artifact_kind is not None:
                params = (*params, artifact_kind)
            cursor = conn.execute(
                "UPDATE audio_urls SET status='done', local_path=?, bundle_path=?, "
                "file_format=?, file_size=?, content_hash=?, downloaded_at=?, "
                "claimed_by='', claimed_at='', lease_expires_at=''"
                f"{artifact_assignment} WHERE url=? AND job_key=?"
                f"{artifact_filter}",
                params,
            )
            if cursor.rowcount != 1:
                raise ValueError("artifact record was not found or its kind did not match")
            conn.commit()
            return False
        except Exception:
            conn.rollback()
            raise

    def update_converted_file(self, old_path: str, new_path: str,
                              file_size: int, content_hash: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE audio_urls SET local_path=?, file_format='opus', file_size=?, "
            "content_hash=? WHERE local_path=? AND artifact_kind='audio'",
            (new_path, file_size, content_hash, old_path),
        )
        conn.commit()
        return cursor.rowcount

    def get_failed(self, limit: int = 50, source: str | None = None,
                   artifact_kind: str | None = None) -> list[dict]:
        """获取 failed 状态的 URL，可按来源过滤"""
        conn = self._get_conn()
        conditions, params = self._filters(
            "failed", source, None, None, None, None, artifact_kind,
        )
        rows = conn.execute(
            f"SELECT * FROM audio_urls WHERE {' AND '.join(conditions)} "
            "ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(r) for r in rows]

    def set_content_hash(self, url: str, content_hash: str):
        conn = self._get_conn()
        conn.execute(
            "UPDATE audio_urls SET content_hash=? WHERE url=? AND job_key=''",
            (content_hash, url),
        )
        conn.commit()

    def hash_exists(self, content_hash: str,
                    artifact_kind: str = "audio") -> bool:
        if not content_hash:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM audio_urls WHERE content_hash=? AND status='done' "
            "AND artifact_kind=?",
            (content_hash, artifact_kind),
        ).fetchone()
        return row is not None

    def source_id_exists(self, source: str, source_id: str,
                         artifact_kind: str = "audio") -> bool:
        if not source_id:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM audio_urls WHERE source=? AND source_id=? "
            "AND artifact_kind=? AND job_key=''",
            (source, source_id, artifact_kind or "audio"),
        ).fetchone()
        return row is not None

    def source_id_prefix_exists(self, source: str, source_id_prefix: str,
                                artifact_kind: str = "audio") -> bool:
        """Return whether any task has the exact stable source-id prefix.

        ``substr`` keeps ``_`` and ``%`` literal, unlike a SQL LIKE pattern.
        This is used to keep a bounded Bilibili batch unique by parent BV even
        when an older task contains a different part or immutable job key.
        """
        if not source_id_prefix:
            return False
        row = self._get_conn().execute(
            "SELECT 1 FROM audio_urls WHERE source=? AND artifact_kind=? "
            "AND substr(source_id, 1, ?) = ? LIMIT 1",
            (
                source,
                artifact_kind or "audio",
                len(source_id_prefix),
                source_id_prefix,
            ),
        ).fetchone()
        return row is not None

    def job_key_exists(self, source: str, artifact_kind: str,
                       job_key: str) -> bool:
        if not job_key:
            return False
        row = self._get_conn().execute(
            "SELECT 1 FROM audio_urls WHERE source=? AND artifact_kind=? "
            "AND job_key=?",
            (source, artifact_kind or "audio", job_key),
        ).fetchone()
        return row is not None

    def url_exists(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM audio_urls WHERE url=? AND job_key=''", (url,),
        ).fetchone()
        return row is not None

    def get_stats(self) -> dict:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT status, COUNT(*) as cnt FROM audio_urls GROUP BY status"
        ).fetchall()
        stats = {r["status"]: r["cnt"] for r in rows}
        stats["total"] = sum(stats.values())

        source_rows = conn.execute(
            "SELECT source, status, COUNT(*) as cnt FROM audio_urls GROUP BY source, status"
        ).fetchall()
        by_source: dict[str, dict[str, int]] = {}
        for r in source_rows:
            by_source.setdefault(r["source"], {})[r["status"]] = r["cnt"]
        stats["by_source"] = by_source

        artifact_rows = conn.execute(
            "SELECT artifact_kind, status, COUNT(*) as cnt FROM audio_urls "
            "GROUP BY artifact_kind, status"
        ).fetchall()
        by_artifact_kind: dict[str, dict[str, int]] = {}
        for r in artifact_rows:
            by_artifact_kind.setdefault(r["artifact_kind"], {})[r["status"]] = r["cnt"]
        stats["by_artifact_kind"] = by_artifact_kind
        return stats

    def get_downloaded_files(self, limit: int = 20,
                             artifact_kind: str | None = None) -> list[dict]:
        conn = self._get_conn()
        artifact_filter = "" if artifact_kind is None else "AND artifact_kind=? "
        params = (limit,) if artifact_kind is None else (artifact_kind, limit)
        rows = conn.execute(
            "SELECT source, title, speaker, category, artifact_kind, job_key, local_path, "
            "bundle_path, downloaded_at "
            "FROM audio_urls WHERE status='done' AND local_path NOT LIKE 'dup:%' "
            f"{artifact_filter}"
            "ORDER BY downloaded_at DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]

    def get_metadata_records(self, limit: int = 10_000,
                             source: str | None = None,
                             missing_only: bool = False,
                             artifact_kind: str | None = None) -> list[dict]:
        """Return formal rows for a metadata audit/backfill without changing state."""
        where = ["1=1"]
        params: list = []
        if source:
            where.append("source=?")
            params.append(source)
        if artifact_kind:
            where.append("artifact_kind=?")
            params.append(artifact_kind)
        if missing_only:
            where.append("COALESCE(metadata_json, '')=''")
        rows = self._get_conn().execute(
            f"SELECT * FROM audio_urls WHERE {' AND '.join(where)} "
            "ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_done_records(self, limit: int = 10_000,
                         source: str | None = None,
                         artifact_kind: str | None = None) -> list[dict]:
        where = "status='done' AND local_path!='' AND local_path NOT LIKE 'dup:%'"
        params: list = []
        if source:
            where += " AND source=?"
            params.append(source)
        if artifact_kind:
            where += " AND artifact_kind=?"
            params.append(artifact_kind)
        rows = self._get_conn().execute(
            f"SELECT * FROM audio_urls WHERE {where} ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def show_stats(self):
        stats = self.get_stats()
        print("\n" + "=" * 62)
        print("  AudioSpider 语音数据统计")
        print("=" * 62)
        print(f"  {'总计 URL:':<12} {stats.get('total', 0)}")
        print(f"  {'待下载:':<12} {stats.get('pending', 0)}")
        print(f"  {'下载中:':<12} {stats.get('downloading', 0)}")
        print(f"  {'已完成:':<12} {stats.get('done', 0)}")
        print(f"  {'失败:':<12} {stats.get('failed', 0)}")

        by_source = stats.get("by_source", {})
        if by_source:
            print("  " + "─" * 58)
            print(f"  {'来源':<16} {'待下载':>6} {'已完成':>6} {'失败':>6}")
            print("  " + "─" * 58)
            for src in sorted(by_source):
                s = by_source[src]
                pending = s.get("pending", 0)
                done = s.get("done", 0)
                failed = s.get("failed", 0)
                print(f"  {src:<16} {pending:>6} {done:>6} {failed:>6}")

        by_artifact_kind = stats.get("by_artifact_kind", {})
        if by_artifact_kind:
            print("  " + "─" * 58)
            print(f"  {'制品类型':<16} {'待处理':>6} {'已完成':>6} {'失败':>6}")
            print("  " + "─" * 58)
            for kind in sorted(by_artifact_kind):
                values = by_artifact_kind[kind]
                print(
                    f"  {kind:<16} {values.get('pending', 0):>6} "
                    f"{values.get('done', 0):>6} {values.get('failed', 0):>6}"
                )

        downloaded = self.get_downloaded_files(10)
        if downloaded:
            print("  " + "─" * 58)
            print("  最近下载:")
            for f in downloaded:
                path = f["local_path"]
                if path and os.path.exists(path):
                    size_mb = os.path.getsize(path) / 1024 / 1024
                    rel = os.path.relpath(path, DOWNLOAD_DIR)
                    print(f"    {rel}")
                    print(f"      {f['title'][:40]} | {size_mb:.1f}MB | {f['downloaded_at'][:16]}")

        disk_total = 0
        file_count = 0
        for root, _, files in os.walk(DOWNLOAD_DIR):
            for fname in files:
                if ".tmp_conv" in fname or ".tmp_convert" in fname:
                    continue
                fp = os.path.join(root, fname)
                try:
                    disk_total += os.path.getsize(fp)
                    file_count += 1
                except OSError:
                    # 转换/下载过程中临时文件可能被删或重命名
                    continue
        print("  " + "─" * 58)
        print(f"  磁盘: {file_count} 个文件, {disk_total / 1024 / 1024:.1f} MB")
        print("=" * 62 + "\n")

    @staticmethod
    def compute_file_hash(filepath: str) -> str:
        h = hashlib.sha256()
        with open(filepath, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
