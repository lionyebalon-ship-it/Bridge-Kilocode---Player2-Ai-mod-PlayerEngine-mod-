"""Per-character memory store, in SQLite.

The bridge persists nothing today — no SQLite, no file writes — so this is the
first persistence layer. It is deliberately tiny: one table, three query shapes,
and hard caps so the store cannot grow without bound.

Two properties matter more than storage size:

**Memories are scoped per character.** A memory from Kilo must never surface for
Blaze, and `query` takes the character explicitly so a caller cannot forget the
scope. That is enforced by the query rather than by caller discipline.

**Rows are capped and expired, not just appended.** `prune` runs on insert, so the
worst-case size is bounded and the pure-Python cosine scan over it stays fast.

Embeddings are stored via the stdlib ``array`` module as float32, which halves the
space of a float64 blob with no quality loss for a unit-normalised comparison.
"""

import sqlite3
import time
from array import array
from pathlib import Path
from typing import Any, Optional

# float32 keeps a 768-dim vector at ~3KB; float64 doubles it for no measurable
# gain in cosine similarity at these magnitudes.
_VECTOR_TYPECODE = "f"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  character_id TEXT NOT NULL,
  text TEXT NOT NULL,
  embedding BLOB NOT NULL,
  ts REAL NOT NULL,
  source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_mem_char_ts ON memories(character_id, ts);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mem_char_text ON memories(character_id, text);
"""


class MemoryStore:
    """Thread-safe-per-connection SQLite store. One instance per bridge."""

    def __init__(self, path: str, max_per_character: int = 500,
                 max_age_days: float = 14.0) -> None:
        self.path = str(path)
        self.max_per_character = _positive_int(max_per_character, 500)
        self.max_age_days = _positive_float(max_age_days, 14.0)
        parent = Path(self.path).parent
        if str(parent) and not parent.exists():
            parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        self._conn.commit()

    def close(self) -> None:
        try:
            self._conn.close()
        except Exception:
            pass

    # --- encoding -----------------------------------------------------------

    def encode_vector(self, vector: Any) -> bytes:
        """Encode a vector as a float32 blob. Raises ValueError on bad input."""
        if not isinstance(vector, (list, tuple)):
            raise ValueError("vector must be a list or tuple")
        if not vector:
            raise ValueError("vector must not be empty")
        try:
            buf = array(_VECTOR_TYPECODE, [float(value) for value in vector])
        except (TypeError, ValueError) as exc:
            raise ValueError(f"vector holds non-numeric values: {exc}") from exc
        return buf.tobytes()

    def decode_vector(self, blob: Any) -> list[float]:
        """Decode a blob back to floats. Returns [] on anything unreadable.

        Note ``array.frombytes`` is in-place and returns ``None`` — wrapping it
        in ``list()`` would raise ``TypeError`` and silently return [] for every
        row, which is exactly how this bug was caught.
        """
        if not isinstance(blob, (bytes, bytearray, memoryview)) or not blob:
            return []
        try:
            buf = array(_VECTOR_TYPECODE)
            buf.frombytes(bytes(blob))
            return list(buf)
        except Exception:
            return []

    # --- writes -------------------------------------------------------------

    def add(self, character_id: str, text: str, embedding: Any,
            source: str = "", ts: Optional[float] = None) -> bool:
        """Store one memory. Returns False (rather than raising) if id or text is unusable.

        A memory that cannot be stored is a memory that cannot be recalled, which
        is degraded, not broken — so the caller is told by the return value.

        Identical text for the same character is skipped: the extraction prompt is
        re-sent on turns where nothing changed, and storing those would fill the
        store with duplicates and crowd out the memories that matter.
        """
        cid = character_id if isinstance(character_id, str) else ""
        body = text.strip() if isinstance(text, str) else ""
        if not cid or not body:
            return False
        try:
            blob = self.encode_vector(embedding)
        except ValueError:
            return False
        when = ts if isinstance(ts, (int, float)) and ts > 0 else time.time()
        try:
            with self._conn:
                self._conn.execute(
                    "INSERT OR IGNORE INTO memories"
                    " (character_id, text, embedding, ts, source)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (cid, body, blob, float(when), source or ""),
                )
            self.prune(cid)
            return True
        except sqlite3.Error:
            return False

    def prune(self, character_id: str) -> int:
        """Drop the oldest rows beyond the cap, and everything past the age cap.

        Runs on insert so the ceiling is enforced before rows accumulate, not on
        a schedule. Returns the number of rows removed.
        """
        cid = character_id if isinstance(character_id, str) else ""
        if not cid:
            return 0
        removed = 0
        try:
            with self._conn:
                if self.max_age_days > 0:
                    cutoff = time.time() - self.max_age_days * 86400.0
                    cur = self._conn.execute(
                        "DELETE FROM memories WHERE character_id = ? AND ts < ?",
                        (cid, cutoff),
                    )
                    removed += cur.rowcount or 0
                if self.max_per_character > 0:
                    cur = self._conn.execute(
                        "DELETE FROM memories WHERE character_id = ? AND id NOT IN ("
                        "  SELECT id FROM memories WHERE character_id = ?"
                        "  ORDER BY ts DESC, id DESC LIMIT ?"
                        ")",
                        (cid, cid, self.max_per_character),
                    )
                    removed += cur.rowcount or 0
        except sqlite3.Error:
            return 0
        return removed

    # --- reads --------------------------------------------------------------

    def count(self, character_id: Optional[str] = None) -> int:
        try:
            if character_id:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM memories WHERE character_id = ?",
                    (character_id,),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT COUNT(*) AS n FROM memories"
                ).fetchone()
            return int(row["n"]) if row else 0
        except sqlite3.Error:
            return 0

    def recent(self, character_id: str, limit: int = 10) -> list[str]:
        """The most recent memory texts for a character, newest first."""
        cid = character_id if isinstance(character_id, str) else ""
        if not cid:
            return []
        try:
            rows = self._conn.execute(
                "SELECT text FROM memories WHERE character_id = ?"
                " ORDER BY ts DESC, id DESC LIMIT ?",
                (cid, _positive_int(limit, 10)),
            ).fetchall()
            return [row["text"] for row in rows]
        except sqlite3.Error:
            return []

    def candidates(self, character_id: str, limit: int = 0):
        """``(vector, text)`` pairs for ranking. Scoped to one character.

        ``limit`` 0 means the cap already applied at insert time; a smaller value
        narrows the scan for a fast path without changing correctness much.
        """
        cid = character_id if isinstance(character_id, str) else ""
        if not cid:
            return []
        try:
            sql = ("SELECT text, embedding FROM memories WHERE character_id = ?"
                   " ORDER BY ts DESC, id DESC")
            params: list[Any] = [cid]
            if limit and limit > 0:
                sql += " LIMIT ?"
                params.append(int(limit))
            rows = self._conn.execute(sql, params).fetchall()
        except sqlite3.Error:
            return []
        out = []
        for row in rows:
            vector = self.decode_vector(row["embedding"])
            if vector:
                out.append((vector, row["text"]))
        return out

    def clear(self, character_id: Optional[str] = None) -> int:
        """Remove everything, or everything for one character."""
        try:
            with self._conn:
                if character_id:
                    cur = self._conn.execute(
                        "DELETE FROM memories WHERE character_id = ?", (character_id,))
                else:
                    cur = self._conn.execute("DELETE FROM memories")
                return cur.rowcount or 0
        except sqlite3.Error:
            return 0


def _positive_int(value: Any, default: int) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int) and value > 0:
        return value
    return default


def _positive_float(value: Any, default: float) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)):
        number = float(value)
        if number > 0:
            return number
    return default
