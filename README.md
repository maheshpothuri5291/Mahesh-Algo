# Trackers

*(Folder, env vars, and `register.db` keep the original "register" name — see note at the bottom.)*

A local tracker for **Tasks**, **Issues**, and **Brainstorming** — FastAPI backend,
SQLite storage, a single-page vanilla-JS frontend. Independent of the algo_app
trading system in the rest of this repo; nothing here touches it.

- **Tasks** / **Issues** — Summary, Description, Assignee (dropdown: Unassigned / Naresh /
  Sandeep / Kishore / Mahesh), Status, Comments
- **Brainstorming** — Summary, Description, MOM (Minutes of Meeting)

Left sidebar leads with a **Dashboard** — Tasks and Issues counts broken down by
status — then the three board tabs below it. Clicking a record opens it in the
right-hand panel to view/edit — changes save only when you click **Save**.
**+ Create** opens a form in a popup, pre-filled with each board's default status
(Not Started for Tasks, Open for Issues); the record is written only when you
click **Create** there. Tasks and Issues each have a status filter (chips) and
an assignee filter (dropdown, default All) above their list. **Export Excel**
downloads a workbook with one sheet per board (Tasks / Issues / Brainstorming),
named after that board. Drag the thin dividers between the sidebar, list, and
detail panel to resize them.

## Run locally

```bash
cd register-app
python -m venv venv
venv\Scripts\activate        # Windows;  source venv/bin/activate on Linux/Mac
pip install -r requirements.txt
python main.py
```

Open http://127.0.0.1:8420 — data is stored in `register.db`, created
automatically next to `main.py` on first run. Back it up by copying that file.

## Deploy on an AWS host

The app reads its host/port/DB location from environment variables, so the
same code runs locally or on a server unchanged:

| Variable | Default | Purpose |
|---|---|---|
| `REGISTER_HOST` | `127.0.0.1` | Bind address — `127.0.0.1` behind nginx, or `0.0.0.0` if exposing the port directly |
| `REGISTER_PORT` | `8420` | Port to listen on |
| `REGISTER_DB_PATH` | `register.db` next to `main.py` | SQLite file location — point this at a persistent path outside the app checkout so redeploys never touch your data |
| `REGISTER_CORS_ORIGINS` | `*` | Comma-separated allowed origins; narrow this if the API is reachable from other origins |
| `REGISTER_AUTH_USER` / `REGISTER_AUTH_PASS` | unset (no login wall) | Set both to require sign-in via a login page before any board/API access |
| `REGISTER_SECRET_KEY` | hash of the above | Signs the session cookie; set explicitly (e.g. `openssl rand -hex 32`) rather than relying on the credential-derived fallback |
| `REGISTER_COOKIE_SECURE` | `0` | Set to `1` once served over HTTPS, so the session cookie gets the `Secure` flag |

Steps:

1. Copy this `register-app/` folder to the server, e.g. `/opt/register-app`.
2. `python3 -m venv venv && venv/bin/pip install -r requirements.txt`
3. `sudo mkdir -p /var/lib/register-app` — persistent home for `register.db`,
   separate from the app checkout.
4. Install the systemd unit: copy `deploy/register-app.service` to
   `/etc/systemd/system/`, then:
   ```bash
   sudo systemctl daemon-reload
   sudo systemctl enable --now register-app
   ```
5. (Optional) Put nginx in front — see `deploy/nginx_register.conf` for a
   starting reverse-proxy config, then add TLS (e.g. `certbot`).

To redeploy a code change: copy the updated files over `/opt/register-app`
(excluding `register.db*` — the systemd `REGISTER_DB_PATH` already keeps the
live data outside that folder) and `sudo systemctl restart register-app`.

## Notes

- Single-user. No login wall by default — set `REGISTER_AUTH_USER`/`REGISTER_AUTH_PASS`
  (see table above) to require sign-in, or put it behind nginx + a VPN/IP allowlist
  if it's reachable from the open internet.
- `sqlite3`'s WAL mode is enabled, so concurrent reads/writes from a couple of
  browser tabs are fine; this isn't built for high-concurrency multi-team use.
- The app was renamed from "The Register" to "Trackers" in the UI, page title,
  and CSV export filename. The folder (`register-app/`), the `REGISTER_*` env
  vars, `register.db`, and the systemd unit filename were left as-is so
  existing deploys/paths/docs referencing them don't break — only cosmetic,
  user-visible text changed.
