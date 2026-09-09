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
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS audio_urls (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                url TEXT UNIQUE NOT NULL,
                source TEXT NOT NULL,
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
                content_hash TEXT DEFAULT '',
                source_id TEXT DEFAULT '',
                published_at TEXT DEFAULT '',
                discovered_at TEXT NOT NULL,
                downloaded_at TEXT DEFAULT '',
                claimed_by TEXT DEFAULT '',
                claimed_at TEXT DEFAULT '',
                lease_expires_at TEXT DEFAULT ''
            );

            CREATE INDEX IF NOT EXISTS idx_status ON audio_urls(status);
            CREATE INDEX IF NOT EXISTS idx_source ON audio_urls(source);
            CREATE INDEX IF NOT EXISTS idx_source_id ON audio_urls(source, source_id);
            CREATE INDEX IF NOT EXISTS idx_content_hash ON audio_urls(content_hash);
            CREATE INDEX IF NOT EXISTS idx_category_status ON audio_urls(category, status);
            CREATE INDEX IF NOT EXISTS idx_language_status ON audio_urls(language, status);
            CREATE INDEX IF NOT EXISTS idx_published_status ON audio_urls(published_at, status);

            CREATE TABLE IF NOT EXISTS crawl_checkpoints (
                source TEXT NOT NULL,
                checkpoint_key TEXT NOT NULL,
                checkpoint_value TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (source, checkpoint_key)
            );
        """)
        # Additive migrations for databases created by earlier releases.
        self._add_column_if_missing(conn, "audio_urls", "published_at", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "claimed_by", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "claimed_at", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "lease_expires_at", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "webpage_url", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "description", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "author", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "cover_url", "TEXT DEFAULT ''")
        self._add_column_if_missing(conn, "audio_urls", "metadata_json", "TEXT DEFAULT ''")
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_lease_expires_at "
            "ON audio_urls(status, lease_expires_at)"
        )
        # Recover only legacy rows that predate leases. Active leased work is
        # recovered by claim methods after the lease expires.
        conn.execute(
            "UPDATE audio_urls SET status='pending' "
            "WHERE status='downloading' AND COALESCE(lease_expires_at, '')=''"
        )
        conn.commit()

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
            record.discovered_at = record.discovered_at or datetime.now().isoformat()
            cursor = conn.execute(
                "INSERT OR IGNORE INTO audio_urls "
                "(url, source, title, file_format, file_size, duration, language, "
                "category, speaker, webpage_url, description, author, cover_url, "
                "metadata_json, status, source_id, published_at, discovered_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (record.url, record.source, record.title, record.file_format,
                 record.file_size, record.duration, record.language,
                 record.category, record.speaker, record.webpage_url,
                 record.description, record.author, record.cover_url,
                 record.metadata_json, record.status, record.source_id,
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
        existing_urls = set()
        urls = [record.url for record in records]
        for offset in range(0, len(urls), 500):
            chunk = urls[offset:offset + 500]
            if not chunk:
                continue
            placeholders = ",".join("?" for _ in chunk)
            existing_urls.update(
                row["url"] for row in conn.execute(
                    f"SELECT url FROM audio_urls WHERE url IN ({placeholders})", chunk,
                )
            )
        existing_source_ids = set()
        for record in records:
            if not record.source_id:
                continue
            row = conn.execute(
                "SELECT source, source_id FROM audio_urls "
                "WHERE source=? AND source_id=? LIMIT 1",
                (record.source, record.source_id),
            ).fetchone()
            if row:
                existing_source_ids.add((row["source"], row["source_id"]))
        existing_records = [
            record for record in records
            if record.url in existing_urls
            or (record.source, record.source_id) in existing_source_ids
        ]
        new_records = [record for record in records if record not in existing_records]
        rows = [
            (r.url, r.source, r.title, r.file_format, r.file_size,
             r.duration, r.language, r.category, r.speaker,
             r.webpage_url, r.description, r.author, r.cover_url, r.metadata_json,
             r.status, r.source_id, r.published_at or "", r.discovered_at or now)
            for r in new_records
        ]
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO audio_urls "
            "(url, source, title, file_format, file_size, duration, language, "
            "category, speaker, webpage_url, description, author, cover_url, "
            "metadata_json, status, source_id, published_at, discovered_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
                record.source_id, record.source_id,
                record.published_at, record.published_at,
                record.url, record.source_id, record.source,
                record.source_id,
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
                "source_id=CASE WHEN ?!='' THEN ? ELSE source_id END, "
                "published_at=CASE WHEN ?!='' THEN ? ELSE published_at END "
                "WHERE url=? OR (?!='' AND source=? AND source_id=?)",
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
    ) -> list[dict]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        if per_source and per_category:
            raise ValueError("per_source and per_category are mutually exclusive")
        group_col = "source" if per_source else "category" if per_category else None
        return self._select_records(
            self._get_conn(), "pending", limit, source, category, language,
            group_col, published_since, published_before,
        )

    @staticmethod
    def _filters(status: str, source: str | None, category: str | None,
                 language: str | None, published_since: str | None,
                 published_before: str | None) -> tuple[list[str], list]:
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
                        published_before: str | None = None) -> list[dict]:
        conditions, params = self._filters(
            status, source, category, language, published_since, published_before,
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
               published_before: str | None = None) -> list[dict]:
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
                published_since, published_before,
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
                      published_before: str | None = None) -> list[dict]:
        if per_source and per_category:
            raise ValueError("per_source and per_category are mutually exclusive")
        group_col = "source" if per_source else "category" if per_category else None
        return self._claim(
            "pending", limit, worker_id, lease_seconds, source, category,
            language, group_col, published_since, published_before,
        )

    def claim_failed(self, limit: int, worker_id: str, lease_seconds: int,
                     source: str | None = None) -> list[dict]:
        return self._claim(
            "failed", limit, worker_id, lease_seconds, source=source,
        )

    def update_status(self, url: str, status: str, local_path: str = ""):
        conn = self._get_conn()
        if status == "done":
            conn.execute(
                "UPDATE audio_urls SET status=?, local_path=?, downloaded_at=?, "
                "claimed_by='', claimed_at='', lease_expires_at='' WHERE url=?",
                (status, local_path, datetime.now().isoformat(), url),
            )
        else:
            conn.execute(
                "UPDATE audio_urls SET status=?, claimed_by='', claimed_at='', "
                "lease_expires_at='' WHERE url=?", (status, url),
            )
        conn.commit()

    def finalize_download(self, url: str, local_path: str, file_format: str,
                          file_size: int, content_hash: str) -> bool:
        """Atomically finalize a download. Return True when it is a duplicate."""
        conn = self._get_conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            duplicate = conn.execute(
                "SELECT 1 FROM audio_urls WHERE content_hash=? AND status='done' "
                "AND url!=? LIMIT 1",
                (content_hash, url),
            ).fetchone()
            stored_path = f"dup:{content_hash}" if duplicate else local_path
            conn.execute(
                "UPDATE audio_urls SET status='done', local_path=?, file_format=?, "
                "file_size=?, content_hash=?, downloaded_at=?, claimed_by='', "
                "claimed_at='', lease_expires_at='' WHERE url=?",
                (stored_path, file_format, file_size, content_hash,
                 datetime.now().isoformat(), url),
            )
            conn.commit()
            return duplicate is not None
        except Exception:
            conn.rollback()
            raise

    def update_converted_file(self, old_path: str, new_path: str,
                              file_size: int, content_hash: str) -> int:
        conn = self._get_conn()
        cursor = conn.execute(
            "UPDATE audio_urls SET local_path=?, file_format='opus', file_size=?, "
            "content_hash=? WHERE local_path=?",
            (new_path, file_size, content_hash, old_path),
        )
        conn.commit()
        return cursor.rowcount

    def get_failed(self, limit: int = 50, source: str | None = None) -> list[dict]:
        """获取 failed 状态的 URL，可按来源过滤"""
        conn = self._get_conn()
        if source:
            rows = conn.execute(
                "SELECT * FROM audio_urls WHERE status='failed' AND source=? "
                "ORDER BY id LIMIT ?",
                (source, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM audio_urls WHERE status='failed' ORDER BY id LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    def set_content_hash(self, url: str, content_hash: str):
        conn = self._get_conn()
        conn.execute("UPDATE audio_urls SET content_hash=? WHERE url=?", (content_hash, url))
        conn.commit()

    def hash_exists(self, content_hash: str) -> bool:
        if not content_hash:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM audio_urls WHERE content_hash=? AND status='done'",
            (content_hash,),
        ).fetchone()
        return row is not None

    def source_id_exists(self, source: str, source_id: str) -> bool:
        if not source_id:
            return False
        conn = self._get_conn()
        row = conn.execute(
            "SELECT 1 FROM audio_urls WHERE source=? AND source_id=?",
            (source, source_id),
        ).fetchone()
        return row is not None

    def url_exists(self, url: str) -> bool:
        conn = self._get_conn()
        row = conn.execute("SELECT 1 FROM audio_urls WHERE url=?", (url,)).fetchone()
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
        return stats

    def get_downloaded_files(self, limit: int = 20) -> list[dict]:
        conn = self._get_conn()
        rows = conn.execute(
            "SELECT source, title, speaker, category, local_path, downloaded_at "
            "FROM audio_urls WHERE status='done' AND local_path NOT LIKE 'dup:%' "
            "ORDER BY downloaded_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_metadata_records(self, limit: int = 10_000,
                             source: str | None = None,
                             missing_only: bool = False) -> list[dict]:
        """Return formal rows for a metadata audit/backfill without changing state."""
        where = ["1=1"]
        params: list = []
        if source:
            where.append("source=?")
            params.append(source)
        if missing_only:
            where.append("COALESCE(metadata_json, '')=''")
        rows = self._get_conn().execute(
            f"SELECT * FROM audio_urls WHERE {' AND '.join(where)} "
            "ORDER BY id LIMIT ?",
            (*params, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_done_records(self, limit: int = 10_000,
                         source: str | None = None) -> list[dict]:
        where = "status='done' AND local_path!='' AND local_path NOT LIKE 'dup:%'"
        params: list = []
        if source:
            where += " AND source=?"
            params.append(source)
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
