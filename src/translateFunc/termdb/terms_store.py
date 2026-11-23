"""SQLite —— term-v1 的唯一写入源。

所有写入（L0 入库、落位回流、复活、衰减、候选池）只经本模块；
JSONL revision 导出与 proper_terms.json 扁平视图都是从 SQLite 派生的只读产物。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterator

from .terms_schema import SCHEMA_VERSION

DB_NAME = "termdb.sqlite3"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS terms (
    term_key            TEXT PRIMARY KEY,
    entity_id           TEXT NOT NULL,
    sense_id            TEXT NOT NULL,
    revision            INTEGER NOT NULL,
    state               TEXT NOT NULL,
    tier                TEXT NOT NULL,
    score               REAL NOT NULL,
    lifecycle           TEXT NOT NULL,
    doc                 TEXT NOT NULL,
    stale               INTEGER NOT NULL DEFAULT 0,
    source              TEXT NOT NULL DEFAULT 'corpus',
    created_revision    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_terms_tier ON terms(tier);
CREATE INDEX IF NOT EXISTS idx_terms_state ON terms(state);

CREATE TABLE IF NOT EXISTS landing_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    term_key    TEXT NOT NULL,
    landing     TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_landing_term ON landing_events(term_key);

CREATE TABLE IF NOT EXISTS build_meta (
    key     TEXT PRIMARY KEY,
    value   TEXT NOT NULL
);
"""


class TermStore:
    """薄封装的 SQLite 存储；同一实例不要跨线程共享连接。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        # 派生库：可从语料确定性重建，关闭 fsync 大幅加速批量入库
        self.conn.execute("PRAGMA journal_mode=MEMORY")
        self.conn.execute("PRAGMA synchronous=OFF")
        self.conn.executescript(SCHEMA_SQL)
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def __enter__(self) -> "TermStore":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()

    # ---- 写入 ----

    def upsert(
        self,
        *,
        term_key: str,
        entity_id: str,
        sense_id: str,
        revision: int,
        state: str,
        tier: str,
        score: float,
        lifecycle: dict,
        doc: dict,
        source: str = "corpus",
        created_revision: str = "",
        keep_if_newer_source: bool = False,
    ) -> bool:
        """写入/更新一条词条。返回 True 表示内容发生变化。"""
        doc_json = json.dumps(doc, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        life_json = json.dumps(lifecycle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        row = self.conn.execute(
            "SELECT doc, lifecycle, source FROM terms WHERE term_key=?", (term_key,)
        ).fetchone()
        if row is not None:
            if keep_if_newer_source and row["source"] == "corpus" and source != "corpus":
                return False
            if row["doc"] == doc_json and row["lifecycle"] == life_json:
                return False
        self.conn.execute(
            """INSERT INTO terms
               (term_key, entity_id, sense_id, revision, state, tier, score,
                lifecycle, doc, stale, source, created_revision)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(term_key) DO UPDATE SET
                 entity_id=excluded.entity_id, sense_id=excluded.sense_id,
                 revision=excluded.revision, state=excluded.state,
                 tier=excluded.tier, score=excluded.score,
                 lifecycle=excluded.lifecycle, doc=excluded.doc,
                 stale=excluded.stale, source=excluded.source,
                 created_revision=excluded.created_revision""",
            (
                term_key, entity_id, sense_id, int(revision), state, tier, float(score),
                life_json, doc_json, int(bool(doc.get("stale", False))),
                source, created_revision,
            ),
        )
        self.conn.commit()
        return True

    def record_landing(self, term_key: str, landing: str, note: str = "") -> int:
        cur = self.conn.execute(
            "INSERT INTO landing_events(term_key, landing, note) VALUES (?,?,?)",
            (term_key, landing, note),
        )
        self.conn.commit()
        return int(cur.lastrowid)

    def landing_history(self, term_key: str, limit: int | None = None) -> list[str]:
        """返回该词条的落位历史（旧→新）；limit 表示取最近 N 条。

        注意：SQLite 的 ORDER BY 只能出现一次，limit 分支必须用独立的完整 SQL，
        不能在基础 SQL 上再拼一段 ORDER BY（会抛 OperationalError）。
        """
        if limit:
            rows = self.conn.execute(
                "SELECT landing FROM landing_events WHERE term_key=? "
                "ORDER BY seq DESC LIMIT ?",
                (term_key, int(limit)),
            ).fetchall()
            return [r["landing"] for r in reversed(rows)]
        rows = self.conn.execute(
            "SELECT landing FROM landing_events WHERE term_key=? ORDER BY seq",
            (term_key,),
        ).fetchall()
        return [r["landing"] for r in rows]

    def delete_source(self, source: str) -> int:
        cur = self.conn.execute("DELETE FROM terms WHERE source=?", (source,))
        self.conn.commit()
        return cur.rowcount

    # ---- 读取 ----

    def get(self, term_key: str) -> dict | None:
        row = self.conn.execute(
            "SELECT doc, lifecycle, tier, state, source FROM terms WHERE term_key=?",
            (term_key,),
        ).fetchone()
        if row is None:
            return None
        return {
            "doc": json.loads(row["doc"]),
            "lifecycle": json.loads(row["lifecycle"]),
            "tier": row["tier"],
            "state": row["state"],
            "source": row["source"],
        }

    def all(self, *, source: str | None = None) -> Iterator[dict]:
        sql = "SELECT doc, lifecycle, tier, state, source FROM terms"
        args: tuple = ()
        if source:
            sql += " WHERE source=?"
            args = (source,)
        sql += " ORDER BY term_key"
        for row in self.conn.execute(sql, args):
            yield {
                "doc": json.loads(row["doc"]),
                "lifecycle": json.loads(row["lifecycle"]),
                "tier": row["tier"],
                "state": row["state"],
                "source": row["source"],
            }

    def count(self) -> dict[str, int]:
        out: dict[str, int] = {}
        for row in self.conn.execute("SELECT tier, COUNT(*) n FROM terms GROUP BY tier"):
            out[f"tier_{row['tier']}"] = int(row["n"])
        for row in self.conn.execute("SELECT state, COUNT(*) n FROM terms GROUP BY state"):
            out[f"state_{row['state']}"] = int(row["n"])
        out["total"] = int(self.conn.execute("SELECT COUNT(*) FROM terms").fetchone()[0])
        return out

    # ---- meta ----

    def set_meta(self, key: str, value: Any) -> None:
        self.conn.execute(
            "INSERT INTO build_meta(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False, sort_keys=True)),
        )
        self.conn.commit()

    def get_meta(self, key: str, default: Any = None) -> Any:
        row = self.conn.execute("SELECT value FROM build_meta WHERE key=?", (key,)).fetchone()
        return json.loads(row["value"]) if row else default
