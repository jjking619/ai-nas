#!/usr/bin/env python3
"""
NAS Knowledge Base API Server
- Indexes file names/paths + text content from /nas_share via SQLite FTS5
- Exposes:  GET  /healthz
            POST /search  {"query": "..."}
            POST /rescan
- Periodic re-scan every SCAN_INTERVAL seconds (no extra packages needed)
"""
import json
import os
import re
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

NAS_ROOT = Path(os.getenv("NAS_ROOT", "/nas_share")).resolve()
PORT = int(os.getenv("PORT", "8084"))
DB_PATH = Path(os.getenv("DB_PATH", "/data/kb.db"))
SCAN_INTERVAL = int(os.getenv("SCAN_INTERVAL", "60"))
MAX_RESULTS = int(os.getenv("MAX_RESULTS", "10"))

# Directories to skip (inside nas_share)
EXCLUDE_DIRS = frozenset(["tools", "Immich上传", ".git", "__pycache__"])
# File suffixes to skip
SKIP_EXTS = frozenset([".immich", ".pyc", ".db", ".js", ".sh", ".service", ".bin", ".so"])
# Extensions whose text content can be read directly
TEXT_EXTS = frozenset([".txt", ".md", ".csv", ".log"])

_db_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(DB_PATH), check_same_thread=False)
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files USING fts5(
            path,
            name,
            ext,
            category,
            content,
            tokenize='unicode61'
        )
    """)
    db.execute("""
        CREATE TABLE IF NOT EXISTS meta (
            path TEXT PRIMARY KEY,
            mtime REAL
        )
    """)
    db.commit()
    return db


# ---------------------------------------------------------------------------
# Scanning
# ---------------------------------------------------------------------------

def _should_skip(rel: Path) -> bool:
    for part in rel.parts:
        if part in EXCLUDE_DIRS or part.startswith("."):
            return True
    return rel.suffix.lower() in SKIP_EXTS


def _read_text(fp: Path) -> str:
    try:
        return fp.read_text(encoding="utf-8", errors="replace")[:8192]
    except Exception:
        return ""


def _extract_content(fp: Path) -> str:
    ext = fp.suffix.lower()
    if ext in TEXT_EXTS:
        return _read_text(fp)
    return ""


def scan(db: sqlite3.Connection) -> int:
    """Incremental scan: insert new/changed, remove deleted files."""
    current: dict[str, float] = {}
    for fp in NAS_ROOT.rglob("*"):
        if not fp.is_file():
            continue
        rel = fp.relative_to(NAS_ROOT)
        if _should_skip(rel):
            continue
        try:
            current[str(rel)] = fp.stat().st_mtime
        except OSError:
            pass

    with _db_lock:
        existing = dict(db.execute("SELECT path, mtime FROM meta").fetchall())

        # Remove deleted
        for p in set(existing) - set(current):
            db.execute("DELETE FROM files WHERE path = ?", (p,))
            db.execute("DELETE FROM meta WHERE path = ?", (p,))

        # Insert / update changed
        changed = 0
        for rel_str, mtime in current.items():
            if rel_str in existing and existing[rel_str] == mtime:
                continue
            fp = NAS_ROOT / rel_str
            rel = Path(rel_str)
            parts = rel.parts
            category = parts[0] if parts else ""
            content = _extract_content(fp)

            db.execute("DELETE FROM files WHERE path = ?", (rel_str,))
            db.execute(
                "INSERT INTO files(path, name, ext, category, content) VALUES (?,?,?,?,?)",
                (rel_str, rel.stem, rel.suffix.lower(), category, content),
            )
            db.execute(
                "INSERT OR REPLACE INTO meta(path, mtime) VALUES (?,?)",
                (rel_str, mtime),
            )
            changed += 1

        db.commit()
        total = db.execute("SELECT COUNT(*) FROM meta").fetchone()[0]

    print(f"[kb] scan done: total={total} changed={changed}")
    return total


def _scan_loop(db: sqlite3.Connection):
    while True:
        time.sleep(SCAN_INTERVAL)
        try:
            scan(db)
        except Exception as e:
            print(f"[kb] scan error: {e}")


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

_FTS_UNSAFE = re.compile(r'["\*\(\)\[\]\{\}:^~]')


def _sanitize_fts(q: str) -> str:
    return _FTS_UNSAFE.sub(" ", q).strip()


def search(db: sqlite3.Connection, query: str, limit: int = MAX_RESULTS) -> list[dict]:
    q = _sanitize_fts(query)
    if not q:
        return []

    results = []
    with _db_lock:
        try:
            rows = db.execute(
                """SELECT path, name, ext, category,
                          snippet(files, 4, '→', '←', '...', 20) AS snip
                   FROM files
                   WHERE files MATCH ?
                   ORDER BY rank
                   LIMIT ?""",
                (q, limit),
            ).fetchall()
            for path, name, ext, category, snip in rows:
                results.append({
                    "path": f"/nas_share/{path}",
                    "name": name,
                    "ext": ext,
                    "category": category,
                    "snippet": snip,
                })
        except sqlite3.OperationalError:
            # FTS syntax error → fallback to LIKE on name
            try:
                rows = db.execute(
                    "SELECT path, name, ext, category, '' FROM files WHERE name LIKE ? LIMIT ?",
                    (f"%{q}%", limit),
                ).fetchall()
                for path, name, ext, category, _ in rows:
                    results.append({
                        "path": f"/nas_share/{path}",
                        "name": name,
                        "ext": ext,
                        "category": category,
                        "snippet": "",
                    })
            except Exception:
                pass

    return results


# ---------------------------------------------------------------------------
# HTTP handler
# ---------------------------------------------------------------------------

def _json(handler: BaseHTTPRequestHandler, status: int, payload: dict):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


class Handler(BaseHTTPRequestHandler):
    db: sqlite3.Connection | None = None

    def log_message(self, fmt, *args):
        pass  # silence access log

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length))
        except Exception:
            return {}

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/healthz":
            with _db_lock:
                count = self.db.execute("SELECT COUNT(*) FROM meta").fetchone()[0]
            _json(self, 200, {"ok": True, "indexed": count})
        elif path == "/search":
            q = parse_qs(urlparse(self.path).query).get("q", [""])[0]
            _json(self, 200, {"ok": True, "query": q, "results": search(self.db, q)})
        else:
            _json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        body = self._read_body()
        if path == "/search":
            q = body.get("query", body.get("q", ""))
            _json(self, 200, {"ok": True, "query": q, "results": search(self.db, q)})
        elif path == "/rescan":
            threading.Thread(target=scan, args=(self.db,), daemon=True).start()
            _json(self, 200, {"ok": True, "message": "rescan triggered"})
        else:
            _json(self, 404, {"ok": False, "error": "not found"})


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    db = open_db()
    Handler.db = db

    print(f"[kb] starting – NAS_ROOT={NAS_ROOT}  port={PORT}")
    scan(db)

    threading.Thread(target=_scan_loop, args=(db,), daemon=True).start()

    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(f"[kb] ready on :{PORT}")
    server.serve_forever()


if __name__ == "__main__":
    main()
