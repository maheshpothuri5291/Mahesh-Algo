"""Trackers — task / issue / brainstorm tracker (FastAPI + SQLite).

Standalone app, independent of algo_app. Runs locally as-is, or on a server —
host/port/DB location are env-configurable for that:

    pip install -r requirements.txt
    python main.py

Local default: http://127.0.0.1:8420, data in register.db next to this file.

Server deploy: set REGISTER_DB_PATH to a persistent path (e.g. an EBS-backed
directory outside the app checkout, so `git pull` / redeploy never touches
it), REGISTER_HOST=0.0.0.0, REGISTER_PORT as needed, and put this behind
nginx/systemd — see deploy/register-app.service and README.md.
"""
import csv
import io
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

BASE_DIR = Path(__file__).parent
DB_PATH = Path(os.getenv("REGISTER_DB_PATH", str(BASE_DIR / "register.db")))
HOST = os.getenv("REGISTER_HOST", "127.0.0.1")
PORT = int(os.getenv("REGISTER_PORT", "8420"))
# Comma-separated list, e.g. "https://register.example.com"; "*" (default) is fine
# behind a same-origin nginx proxy but should be narrowed if the API is ever
# exposed directly to other origins.
CORS_ORIGINS = os.getenv("REGISTER_CORS_ORIGINS", "*").split(",")

# Field set per board — brainstorming has no assignee/status/comments, it has mom instead.
TYPE_META = {
    "tasks":         {"code": "TSK", "fields": ["summary", "description", "assignee", "status", "comments"]},
    "issues":        {"code": "ISS", "fields": ["summary", "description", "assignee", "status", "comments"]},
    "brainstorming": {"code": "BRN", "fields": ["summary", "description", "mom"]},
}


@contextmanager
def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_conn() as conn:
        conn.execute("""CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            assignee TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS issues (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            assignee TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT '',
            comments TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS brainstorming (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            summary TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            mom TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )""")
        conn.commit()


def _code(type_: str, item_id: int) -> str:
    return f"{TYPE_META[type_]['code']}-{item_id:03d}"


def _row_to_dict(type_: str, row: sqlite3.Row) -> dict:
    d = {"id": row["id"], "code": _code(type_, row["id"]),
         "created_at": row["created_at"], "updated_at": row["updated_at"]}
    for f in TYPE_META[type_]["fields"]:
        d[f] = row[f]
    return d


def _require_type(type_: str) -> None:
    if type_ not in TYPE_META:
        raise HTTPException(404, f"Unknown board: {type_!r}")


class ItemIn(BaseModel):
    summary: str = ""
    description: str = ""
    assignee: Optional[str] = ""
    status: Optional[str] = ""
    comments: Optional[str] = ""
    mom: Optional[str] = ""


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    yield


app = FastAPI(title="Trackers", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[o.strip() for o in CORS_ORIGINS],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/export.csv")
def export_csv():
    """One combined CSV across all three boards — a 'Tab' column identifies
    which board each row came from, per the union of all boards' fields.

    Registered ahead of GET /api/{type_} below: Starlette matches routes in
    declaration order, and {type_} is a single path segment that would
    otherwise swallow "export.csv" as an (invalid) board name."""
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Tab", "ID", "Summary", "Description", "Assignee", "Status",
                      "Comments", "MOM", "Created", "Updated"])
    with get_conn() as conn:
        for type_, meta in TYPE_META.items():
            rows = conn.execute(f"SELECT * FROM {type_} ORDER BY id").fetchall()
            for row in rows:
                keys = row.keys()
                writer.writerow([
                    type_.capitalize(),
                    _code(type_, row["id"]),
                    row["summary"],
                    row["description"],
                    row["assignee"] if "assignee" in keys else "",
                    row["status"] if "status" in keys else "",
                    row["comments"] if "comments" in keys else "",
                    row["mom"] if "mom" in keys else "",
                    row["created_at"],
                    row["updated_at"],
                ])
    stamp = datetime.now().strftime("%Y-%m-%d")
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="trackers-{stamp}.csv"'},
    )


@app.get("/api/{type_}")
def list_items(type_: str):
    _require_type(type_)
    with get_conn() as conn:
        rows = conn.execute(f"SELECT * FROM {type_} ORDER BY updated_at DESC").fetchall()
    return [_row_to_dict(type_, r) for r in rows]


@app.post("/api/{type_}")
def create_item(type_: str, body: ItemIn):
    _require_type(type_)
    fields = TYPE_META[type_]["fields"]
    now = datetime.now(timezone.utc).isoformat()
    values = [getattr(body, f) or "" for f in fields]
    cols = ", ".join(fields + ["created_at", "updated_at"])
    placeholders = ", ".join(["?"] * (len(fields) + 2))
    with get_conn() as conn:
        cur = conn.execute(
            f"INSERT INTO {type_} ({cols}) VALUES ({placeholders})",
            [*values, now, now],
        )
        conn.commit()
        row = conn.execute(f"SELECT * FROM {type_} WHERE id=?", (cur.lastrowid,)).fetchone()
    return _row_to_dict(type_, row)


@app.put("/api/{type_}/{item_id}")
def update_item(type_: str, item_id: int, body: ItemIn):
    _require_type(type_)
    fields = TYPE_META[type_]["fields"]
    now = datetime.now(timezone.utc).isoformat()
    values = [getattr(body, f) or "" for f in fields]
    set_clause = ", ".join([f"{f}=?" for f in fields] + ["updated_at=?"])
    with get_conn() as conn:
        cur = conn.execute(
            f"UPDATE {type_} SET {set_clause} WHERE id=?",
            [*values, now, item_id],
        )
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
        row = conn.execute(f"SELECT * FROM {type_} WHERE id=?", (item_id,)).fetchone()
    return _row_to_dict(type_, row)


@app.delete("/api/{type_}/{item_id}")
def delete_item(type_: str, item_id: int):
    _require_type(type_)
    with get_conn() as conn:
        cur = conn.execute(f"DELETE FROM {type_} WHERE id=?", (item_id,))
        conn.commit()
        if cur.rowcount == 0:
            raise HTTPException(404, "Not found")
    return {"ok": True}


# Static frontend — mounted last so /api/* above takes priority.
app.mount("/", StaticFiles(directory=BASE_DIR / "static", html=True), name="static")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=HOST, port=PORT)
