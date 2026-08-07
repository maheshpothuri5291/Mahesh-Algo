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
import hashlib
import hmac
import io
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, StreamingResponse
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

# Optional login-page auth. Unset (the local-dev default) => no login wall, same
# as before. Set REGISTER_AUTH_USER + REGISTER_AUTH_PASS to require sign-in.
AUTH_USER = os.getenv("REGISTER_AUTH_USER", "")
AUTH_PASS = os.getenv("REGISTER_AUTH_PASS", "")
AUTH_ENABLED = bool(AUTH_USER and AUTH_PASS)
# Signs the session cookie. Set REGISTER_SECRET_KEY explicitly in production;
# falling back to a hash of the credentials keeps things working either way,
# but a dedicated secret means the cookie can't be derived from a guessed password.
AUTH_SECRET = os.getenv("REGISTER_SECRET_KEY") or hashlib.sha256(f"{AUTH_USER}:{AUTH_PASS}".encode()).hexdigest()
# Set REGISTER_COOKIE_SECURE=1 once the app is served over HTTPS.
COOKIE_SECURE = os.getenv("REGISTER_COOKIE_SECURE", "0") == "1"
COOKIE_NAME = "register_session"
COOKIE_MAX_AGE = 60 * 60 * 24 * 30  # 30 days

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


class LoginIn(BaseModel):
    username: str = ""
    password: str = ""


def _session_token() -> str:
    """Stateless token: valid as long as it matches, no server-side session store."""
    return hmac.new(AUTH_SECRET.encode(), AUTH_USER.encode(), hashlib.sha256).hexdigest()


def _token_valid(token: Optional[str]) -> bool:
    return bool(token) and hmac.compare_digest(token, _session_token())


LOGIN_PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Trackers &middot; Sign in</title>
<style>
  :root {
    --bg: #ffffff; --surface: #ffffff; --surface-2: #f4f5f7; --border: #dfe2e6;
    --border-strong: #c2c7cd; --ink: #1a2027; --ink-dim: #5a6572; --ink-faint: #8b96a3;
    --tasks: #2f63b8; --crit: #c0433f;
    --shadow: 0 1px 2px rgba(26,32,39,0.06), 0 8px 20px rgba(26,32,39,0.07);
    --radius: 8px;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; min-height: 100vh; display: flex; align-items: center; justify-content: center;
    background: var(--surface-2); color: var(--ink);
    font-family: -apple-system, "Segoe UI", ui-sans-serif, system-ui, Roboto, sans-serif;
  }
  .card {
    width: 320px; background: var(--surface); border: 1px solid var(--border);
    border-radius: var(--radius); box-shadow: var(--shadow); padding: 28px 26px;
  }
  h1 { margin: 0 0 4px; font-size: 19px; }
  p.sub { margin: 0 0 20px; color: var(--ink-dim); font-size: 13px; }
  label { display: block; font-size: 12.5px; color: var(--ink-dim); margin: 14px 0 5px; }
  input {
    width: 100%; padding: 8px 10px; font-size: 14px; border: 1px solid var(--border-strong);
    border-radius: 6px; background: var(--surface); color: var(--ink);
  }
  input:focus { outline: 2px solid var(--tasks); outline-offset: -1px; }
  button {
    width: 100%; margin-top: 20px; padding: 9px 10px; font-size: 14px; font-weight: 600;
    color: #fff; background: var(--tasks); border: none; border-radius: 6px; cursor: pointer;
  }
  button:disabled { opacity: 0.6; cursor: default; }
  .error {
    margin-top: 14px; font-size: 13px; color: var(--crit); display: none;
  }
</style>
</head>
<body>
  <form class="card" id="login-form">
    <h1>Trackers</h1>
    <p class="sub">Sign in to continue</p>
    <label for="username">Username</label>
    <input id="username" name="username" autocomplete="username" autofocus required>
    <label for="password">Password</label>
    <input id="password" name="password" type="password" autocomplete="current-password" required>
    <button type="submit" id="submit-btn">Sign in</button>
    <div class="error" id="error">Incorrect username or password.</div>
  </form>
<script>
(function () {
  "use strict";
  var form = document.getElementById("login-form");
  var errorEl = document.getElementById("error");
  var btn = document.getElementById("submit-btn");
  form.addEventListener("submit", function (e) {
    e.preventDefault();
    errorEl.style.display = "none";
    btn.disabled = true;
    btn.textContent = "Signing in…";
    fetch("/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: document.getElementById("username").value,
        password: document.getElementById("password").value
      })
    }).then(function (res) {
      if (res.ok) {
        var params = new URLSearchParams(window.location.search);
        window.location.href = params.get("next") || "/";
      } else {
        errorEl.style.display = "block";
        btn.disabled = false;
        btn.textContent = "Sign in";
      }
    }).catch(function () {
      errorEl.style.display = "block";
      btn.disabled = false;
      btn.textContent = "Sign in";
    });
  });
})();
</script>
</body>
</html>"""


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


@app.middleware("http")
async def auth_guard(request: Request, call_next):
    """Login-page wall. No-op entirely when REGISTER_AUTH_USER/PASS aren't set."""
    if not AUTH_ENABLED or request.url.path in ("/login", "/logout"):
        return await call_next(request)
    if _token_valid(request.cookies.get(COOKIE_NAME)):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "Not authenticated"}, status_code=401)
    next_path = request.url.path
    return RedirectResponse(f"/login?next={next_path}", status_code=303)


@app.get("/login")
def login_page(request: Request):
    if not AUTH_ENABLED or _token_valid(request.cookies.get(COOKIE_NAME)):
        return RedirectResponse("/")
    return HTMLResponse(LOGIN_PAGE)


@app.post("/login")
def login_submit(body: LoginIn):
    if not AUTH_ENABLED:
        return {"ok": True}
    if not (hmac.compare_digest(body.username, AUTH_USER) and hmac.compare_digest(body.password, AUTH_PASS)):
        raise HTTPException(401, "Incorrect username or password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        COOKIE_NAME, _session_token(),
        max_age=COOKIE_MAX_AGE, httponly=True, samesite="lax", secure=COOKIE_SECURE,
    )
    return resp


@app.get("/logout")
def logout():
    resp = RedirectResponse("/login")
    resp.delete_cookie(COOKIE_NAME)
    return resp


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
