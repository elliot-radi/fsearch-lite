"""Core indexing and search logic for fsearch-lite. No GUI dependencies here
so this can be tested, or run headless via --reindex, without GTK installed."""
import json
import os
import re
import sqlite3

CONFIG_DIR = os.path.expanduser("~/.config/fsearch-lite")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
DB_PATH = os.path.join(CONFIG_DIR, "index.db")

DEFAULT_EXCLUDES = {".git", "node_modules", "__pycache__", ".cache", "venv", ".venv", "$RECYCLE.BIN"}


def load_config():
    os.makedirs(CONFIG_DIR, exist_ok=True)
    if not os.path.exists(CONFIG_PATH):
        cfg = {"roots": [os.path.expanduser("~")], "excludes": sorted(DEFAULT_EXCLUDES)}
        save_config(cfg)
        return cfg
    with open(CONFIG_PATH) as f:
        return json.load(f)


def save_config(cfg):
    with open(CONFIG_PATH, "w") as f:
        json.dump(cfg, f, indent=2)


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            dir TEXT NOT NULL,
            path TEXT NOT NULL UNIQUE,
            size INTEGER,
            mtime REAL,
            is_dir INTEGER
        )
    """)
    # Default unicode61 tokenizer treats '_', '-', '.' as separators, which is
    # what we want: "report_final.docx" tokenizes to ["report","final","docx"]
    # so a plain multi-term AND search matches on any of those words.
    conn.execute("""
        CREATE VIRTUAL TABLE IF NOT EXISTS files_fts USING fts5(
            name, content='files', content_rowid='id',
            tokenize="unicode61"
        )
    """)

    def regexp(pattern, value):
        if value is None:
            return False
        try:
            return re.search(pattern, value, re.IGNORECASE) is not None
        except re.error:
            return False

    conn.create_function("REGEXP", 2, regexp)
    return conn


def build_index(roots, excludes, progress_cb=None):
    """Full rescan. progress_cb(count) called periodically if given."""
    conn = get_db()
    cur = conn.cursor()
    cur.execute("DELETE FROM files")
    cur.execute("DELETE FROM files_fts")

    excludes = set(excludes)
    batch = []
    count = 0

    def flush():
        nonlocal batch
        if not batch:
            return
        cur.executemany(
            "INSERT INTO files (name, dir, path, size, mtime, is_dir) VALUES (?,?,?,?,?,?)",
            batch,
        )
        batch = []

    for root in roots:
        root = os.path.expanduser(root)
        if not os.path.isdir(root):
            continue
        stack = [root]
        while stack:
            current = stack.pop()
            try:
                with os.scandir(current) as it:
                    for entry in it:
                        if entry.name in excludes:
                            continue
                        try:
                            st = entry.stat(follow_symlinks=False)
                        except OSError:
                            continue
                        is_dir = entry.is_dir(follow_symlinks=False)
                        batch.append((
                            entry.name,
                            current,
                            entry.path,
                            st.st_size,
                            st.st_mtime,
                            1 if is_dir else 0,
                        ))
                        count += 1
                        if len(batch) >= 2000:
                            flush()
                            if progress_cb:
                                progress_cb(count)
                        if is_dir:
                            stack.append(entry.path)
            except OSError:
                continue
    flush()
    conn.commit()

    cur.execute("INSERT INTO files_fts(files_fts) VALUES('rebuild')")
    conn.commit()
    conn.close()
    return count


def delete_indexed_path(conn, path):
    """Remove a single path from the index (used when a file is trashed
    from the GUI, so the index doesn't need a full reindex to stay accurate)."""
    row = conn.execute("SELECT id, name FROM files WHERE path=?", (path,)).fetchone()
    if row is None:
        return
    file_id, name = row
    conn.execute(
        "INSERT INTO files_fts(files_fts, rowid, name) VALUES('delete', ?, ?)",
        (file_id, name),
    )
    conn.execute("DELETE FROM files WHERE id=?", (file_id,))
    conn.commit()


def _escape_like(term):
    """Escape %, _, and \\ so a term is matched as a literal substring in LIKE."""
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _escape_fts_phrase(term):
    """Escape a term for use inside FTS5 double-quoted phrase syntax."""
    return term.replace('"', '""')


def search(conn, query, regex_mode, sort_col, sort_desc, limit=2000):
    order = {
        "name": "files.name",
        "dir": "files.dir",
        "size": "files.size",
        "mtime": "files.mtime",
    }.get(sort_col, "files.name")
    direction = "DESC" if sort_desc else "ASC"

    if not query.strip():
        sql = f"SELECT name, dir, path, size, mtime, is_dir FROM files ORDER BY {order} {direction} LIMIT ?"
        return conn.execute(sql, (limit,)).fetchall()

    if regex_mode:
        sql = f"""
            SELECT name, dir, path, size, mtime, is_dir FROM files
            WHERE name REGEXP ? OR path REGEXP ?
            ORDER BY {order} {direction} LIMIT ?
        """
        return conn.execute(sql, (query, query, limit)).fetchall()

    terms = [t for t in query.strip().split() if t]

    # The FTS5 tokenizer splits on '_', '-', '.' as word separators, which is
    # what lets "report final" match "report_final.docx" — but it also means
    # FTS can't distinguish "mes_" from "mes", since the separator itself is
    # discarded before indexing. So: use FTS for fast candidate narrowing,
    # then re-check each term as a literal case-insensitive substring of the
    # actual filename, which does respect '_'/'-'/'.' exactly as typed.
    match_expr = " ".join(f'"{_escape_fts_phrase(t)}"*' for t in terms)
    like_clauses = " AND ".join("files.name LIKE ? ESCAPE '\\'" for _ in terms)
    like_params = [f"%{_escape_like(t)}%" for t in terms]

    sql = f"""
        SELECT files.name, files.dir, files.path, files.size, files.mtime, files.is_dir
        FROM files_fts
        JOIN files ON files.id = files_fts.rowid
        WHERE files_fts MATCH ? AND {like_clauses}
        ORDER BY {order} {direction} LIMIT ?
    """
    try:
        return conn.execute(sql, [match_expr] + like_params + [limit]).fetchall()
    except sqlite3.OperationalError:
        # malformed FTS query -> fall back to substring-only search
        sql = f"""
            SELECT name, dir, path, size, mtime, is_dir FROM files
            WHERE {like_clauses}
            ORDER BY {order} {direction} LIMIT ?
        """
        return conn.execute(sql, like_params + [limit]).fetchall()
