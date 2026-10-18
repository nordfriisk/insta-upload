#!/usr/bin/env python3
"""Zeitgesteuertes Veröffentlichen von Instagram-Posts, -Reels und -Stories.

Das Skript liest geplante Beiträge aus einer CSV-Datei und veröffentlicht alle
fälligen Einträge über die offizielle Instagram Graph API. Die Medien müssen
öffentlich erreichbar sein (z. B. bei Infomaniak); die Graph API lädt sie selbst
von dort. Welche Beiträge schon veröffentlicht wurden, steht in einer SQLite-DB.

Gedacht für den Aufruf per Cron, z. B. alle 5 Minuten:

    */5 * * * * cd /pfad/zu/insta-upload && ./venv/bin/python insta_upload.py run
"""

from __future__ import annotations

import argparse
import csv
import fcntl
import os
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests

VIDEO_EXTENSIONS = {".mp4", ".mov"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png"}
POST_TYPES = {"post", "story", "reel"}

SCHEMA = """
CREATE TABLE IF NOT EXISTS publications (
    id           TEXT PRIMARY KEY,
    type         TEXT NOT NULL,
    scheduled_at TEXT NOT NULL,
    status       TEXT NOT NULL,          -- 'published' | 'failed'
    attempts     INTEGER NOT NULL DEFAULT 0,
    media_id     TEXT,
    last_error   TEXT,
    updated_at   TEXT NOT NULL
);
"""


# --------------------------------------------------------------------------- #
# Konfiguration
# --------------------------------------------------------------------------- #

def load_dotenv(path: Path) -> None:
    """Liest KEY=VALUE-Zeilen aus einer .env-Datei. Bestehende Umgebungsvariablen gewinnen."""
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


@dataclass
class Config:
    ig_user_id: str
    access_token: str
    media_base_url: str
    schedule_file: Path
    state_db: Path
    timezone: ZoneInfo
    graph_host: str = "https://graph.facebook.com"
    graph_version: str = "v24.0"
    max_attempts: int = 3
    poll_interval: float = 5.0
    poll_timeout: float = 300.0

    @classmethod
    def from_env(cls, base_dir: Path) -> "Config":
        missing = [k for k in ("IG_USER_ID", "IG_ACCESS_TOKEN", "MEDIA_BASE_URL") if not os.environ.get(k)]
        if missing:
            raise SystemExit(f"Fehlende Einstellungen in .env: {', '.join(missing)}")
        base_url = os.environ["MEDIA_BASE_URL"]
        if not base_url.endswith("/"):
            base_url += "/"
        return cls(
            ig_user_id=os.environ["IG_USER_ID"],
            access_token=os.environ["IG_ACCESS_TOKEN"],
            media_base_url=base_url,
            schedule_file=base_dir / os.environ.get("SCHEDULE_FILE", "posts.csv"),
            state_db=base_dir / os.environ.get("STATE_DB", "state.db"),
            timezone=ZoneInfo(os.environ.get("TIMEZONE", "Europe/Berlin")),
            graph_host=os.environ.get("GRAPH_HOST", cls.graph_host).rstrip("/"),
            graph_version=os.environ.get("GRAPH_API_VERSION", cls.graph_version),
            max_attempts=int(os.environ.get("MAX_ATTEMPTS", cls.max_attempts)),
        )


# --------------------------------------------------------------------------- #
# Zeitplan (CSV)
# --------------------------------------------------------------------------- #

@dataclass
class ScheduledPost:
    id: str
    publish_at: datetime
    type: str
    media: list[str]
    caption: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def is_carousel(self) -> bool:
        return self.type == "post" and len(self.media) > 1


def is_video(name: str) -> bool:
    return Path(name.split("?", 1)[0]).suffix.lower() in VIDEO_EXTENSIONS


def media_url(base_url: str, name: str) -> str:
    """Volle URLs bleiben unverändert, Dateinamen werden an MEDIA_BASE_URL angehängt."""
    if name.startswith(("http://", "https://")):
        return name
    return urljoin(base_url, name.lstrip("/"))


def parse_datetime(value: str, tz: ZoneInfo) -> datetime:
    dt = datetime.fromisoformat(value.strip())
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=tz)
    return dt


def validate(post: ScheduledPost) -> list[str]:
    errors = []
    if post.type not in POST_TYPES:
        errors.append(f"unbekannter Typ '{post.type}' (erlaubt: {', '.join(sorted(POST_TYPES))})")
    if not post.media:
        errors.append("keine Mediendatei angegeben")
    for name in post.media:
        ext = Path(name.split("?", 1)[0]).suffix.lower()
        if ext not in VIDEO_EXTENSIONS | IMAGE_EXTENSIONS:
            errors.append(f"nicht unterstütztes Format: {name}")
    if post.type in {"story", "reel"} and len(post.media) > 1:
        errors.append(f"ein {post.type} darf nur eine Datei haben")
    if post.type == "reel" and post.media and not is_video(post.media[0]):
        errors.append("ein Reel braucht ein Video (.mp4/.mov)")
    if post.is_carousel and len(post.media) > 10:
        errors.append("ein Karussell darf höchstens 10 Dateien haben")
    if len(post.caption) > 2200:
        errors.append("Bildunterschrift länger als 2200 Zeichen")
    return errors


def read_schedule(path: Path, tz: ZoneInfo) -> list[ScheduledPost]:
    """Erwartete Spalten: id, publish_at, type, media, caption.

    `media` enthält einen oder mehrere Dateinamen, getrennt durch '|'.
    """
    posts = []
    seen: set[str] = set()
    with path.open(newline="", encoding="utf-8-sig") as fh:
        for line_no, row in enumerate(csv.DictReader(fh), start=2):
            row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
            post_id = row.get("id", "")
            errors = []
            try:
                publish_at = parse_datetime(row.get("publish_at", ""), tz)
            except ValueError:
                publish_at = datetime.max.replace(tzinfo=timezone.utc)
                errors.append(f"ungültiges Datum '{row.get('publish_at', '')}'")
            post = ScheduledPost(
                id=post_id,
                publish_at=publish_at,
                type=row.get("type", "post").lower() or "post",
                media=[m.strip() for m in row.get("media", "").split("|") if m.strip()],
                caption=row.get("caption", ""),
            )
            if not post_id:
                errors.append("Spalte 'id' ist leer")
            elif post_id in seen:
                errors.append(f"id '{post_id}' kommt doppelt vor")
            seen.add(post_id)
            post.errors = [f"Zeile {line_no}: {e}" for e in errors + validate(post)]
            posts.append(post)
    return posts


# --------------------------------------------------------------------------- #
# Zustand (SQLite)
# --------------------------------------------------------------------------- #

class State:
    def __init__(self, path: Path):
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.executescript(SCHEMA)

    def get(self, post_id: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM publications WHERE id = ?", (post_id,)).fetchone()

    def record(self, post: ScheduledPost, status: str, media_id: str | None = None, error: str | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        self.db.execute(
            """
            INSERT INTO publications (id, type, scheduled_at, status, attempts, media_id, last_error, updated_at)
            VALUES (?, ?, ?, ?, 1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                status = excluded.status,
                attempts = publications.attempts + 1,
                media_id = COALESCE(excluded.media_id, publications.media_id),
                last_error = excluded.last_error,
                updated_at = excluded.updated_at
            """,
            (post.id, post.type, post.publish_at.isoformat(), status, media_id, error, now),
        )
        self.db.commit()

    def reset(self, post_id: str) -> bool:
        cur = self.db.execute("DELETE FROM publications WHERE id = ? AND status != 'published'", (post_id,))
        self.db.commit()
        return cur.rowcount > 0


# --------------------------------------------------------------------------- #
# Instagram Graph API
# --------------------------------------------------------------------------- #

class GraphError(RuntimeError):
    pass


class GraphClient:
    def __init__(self, cfg: Config, session: requests.Session | None = None, sleep=time.sleep):
        self.cfg = cfg
        self.session = session or requests.Session()
        self.sleep = sleep
        self.base = f"{cfg.graph_host}/{cfg.graph_version}"

    def _request(self, method: str, path: str, **params) -> dict:
        params["access_token"] = self.cfg.access_token
        if method == "GET":
            resp = self.session.get(f"{self.base}/{path}", params=params, timeout=60)
        else:
            resp = self.session.post(f"{self.base}/{path}", data=params, timeout=60)
        try:
            body = resp.json()
        except ValueError:
            raise GraphError(f"HTTP {resp.status_code}: {resp.text[:200]}") from None
        if "error" in body:
            err = body["error"]
            raise GraphError(f"{err.get('type', 'Fehler')} ({err.get('code')}): {err.get('message')}")
        return body

    def create_container(self, **params) -> str:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        return self._request("POST", f"{self.cfg.ig_user_id}/media", **params)["id"]

    def wait_until_ready(self, container_id: str) -> None:
        """Videos (und manchmal Bilder) werden asynchron verarbeitet; erst bei FINISHED veröffentlichen."""
        waited = 0.0
        while True:
            status = self._request("GET", container_id, fields="status_code,status").get("status_code")
            if status in (None, "FINISHED", "PUBLISHED"):
                return
            if status in ("ERROR", "EXPIRED"):
                raise GraphError(f"Container {container_id} meldet Status {status}")
            if waited >= self.cfg.poll_timeout:
                raise GraphError(f"Container {container_id} nach {int(waited)} s noch nicht fertig ({status})")
            self.sleep(self.cfg.poll_interval)
            waited += self.cfg.poll_interval

    def publish(self, container_id: str) -> str:
        return self._request("POST", f"{self.cfg.ig_user_id}/media_publish", creation_id=container_id)["id"]


def media_params(url: str, video: bool) -> dict:
    return {"video_url": url} if video else {"image_url": url}


def publish_post(client: GraphClient, post: ScheduledPost, base_url: str) -> str:
    """Erstellt die Container für einen Beitrag, wartet auf die Verarbeitung und veröffentlicht ihn."""
    urls = [media_url(base_url, m) for m in post.media]

    if post.type == "story":
        video = is_video(post.media[0])
        container = client.create_container(media_type="STORIES", **media_params(urls[0], video))
    elif post.type == "reel" or (post.type == "post" and len(urls) == 1 and is_video(post.media[0])):
        # Feed-Videos laufen bei Instagram nur noch als Reels.
        container = client.create_container(
            media_type="REELS", video_url=urls[0], caption=post.caption, share_to_feed="true"
        )
    elif post.is_carousel:
        children = []
        for name, url in zip(post.media, urls):
            video = is_video(name)
            child = client.create_container(
                is_carousel_item="true", media_type="VIDEO" if video else None, **media_params(url, video)
            )
            client.wait_until_ready(child)
            children.append(child)
        container = client.create_container(media_type="CAROUSEL", children=",".join(children), caption=post.caption)
    else:
        container = client.create_container(image_url=urls[0], caption=post.caption)

    client.wait_until_ready(container)
    return client.publish(container)


# --------------------------------------------------------------------------- #
# Befehle
# --------------------------------------------------------------------------- #

def log(msg: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {msg}", flush=True)


def due_posts(posts: list[ScheduledPost], state: State, now: datetime, max_attempts: int) -> list[ScheduledPost]:
    due = []
    for post in sorted(posts, key=lambda p: p.publish_at):
        if post.errors or post.publish_at > now:
            continue
        row = state.get(post.id)
        if row and (row["status"] == "published" or row["attempts"] >= max_attempts):
            continue
        due.append(post)
    return due


def cmd_run(cfg: Config, dry_run: bool = False, client: GraphClient | None = None) -> int:
    lock_path = cfg.state_db.with_suffix(".lock")
    with open(lock_path, "w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            log("Ein anderer Lauf ist noch aktiv, breche ab.")
            return 0

        posts = read_schedule(cfg.schedule_file, cfg.timezone)
        for post in posts:
            for err in post.errors:
                log(f"WARNUNG {err}")
        state = State(cfg.state_db)
        client = client or GraphClient(cfg)
        failures = 0

        for post in due_posts(posts, state, datetime.now(timezone.utc), cfg.max_attempts):
            if dry_run:
                log(f"[dry-run] würde veröffentlichen: {post.id} ({post.type}, {len(post.media)} Datei(en))")
                continue
            try:
                media_id = publish_post(client, post, cfg.media_base_url)
            except (GraphError, requests.RequestException) as exc:
                failures += 1
                state.record(post, "failed", error=str(exc))
                log(f"FEHLER {post.id}: {exc}")
            else:
                state.record(post, "published", media_id=media_id)
                log(f"OK {post.id} veröffentlicht (Media-ID {media_id})")
        return 1 if failures else 0


def cmd_list(cfg: Config) -> int:
    posts = read_schedule(cfg.schedule_file, cfg.timezone)
    state = State(cfg.state_db)
    print(f"{'ID':<20} {'Zeitpunkt':<17} {'Typ':<6} {'Dateien':>7}  Status")
    for post in sorted(posts, key=lambda p: p.publish_at):
        row = state.get(post.id)
        if post.errors:
            status = "UNGÜLTIG"
        elif row is None:
            status = "geplant"
        elif row["status"] == "published":
            status = f"veröffentlicht ({row['media_id']})"
        else:
            status = f"fehlgeschlagen {row['attempts']}x: {row['last_error']}"
        when = post.publish_at.astimezone(cfg.timezone).strftime("%Y-%m-%d %H:%M") if not post.errors else "-"
        print(f"{post.id:<20} {when:<17} {post.type:<6} {len(post.media):>7}  {status}")
    return 0


def cmd_check(cfg: Config, session: requests.Session | None = None) -> int:
    """Prüft die CSV und ob jede Mediendatei öffentlich abrufbar ist."""
    session = session or requests.Session()
    problems = 0
    for post in read_schedule(cfg.schedule_file, cfg.timezone):
        for err in post.errors:
            print(f"✗ {err}")
            problems += 1
        for name in post.media:
            url = media_url(cfg.media_base_url, name)
            try:
                resp = session.head(url, allow_redirects=True, timeout=20)
                ok = resp.status_code == 200
                detail = f"HTTP {resp.status_code}, {resp.headers.get('Content-Type', '?')}"
            except requests.RequestException as exc:
                ok, detail = False, str(exc)
            print(f"{'✓' if ok else '✗'} {post.id}: {url} ({detail})")
            problems += 0 if ok else 1
    print("Alles in Ordnung." if not problems else f"{problems} Problem(e) gefunden.")
    return 1 if problems else 0


def cmd_retry(cfg: Config, post_id: str) -> int:
    if State(cfg.state_db).reset(post_id):
        print(f"{post_id} wird beim nächsten Lauf erneut versucht.")
        return 0
    print(f"{post_id}: kein fehlgeschlagener Eintrag gefunden.")
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Geplante Instagram-Beiträge veröffentlichen.")
    parser.add_argument("--env", type=Path, default=Path(__file__).with_name(".env"), help="Pfad zur .env-Datei")
    sub = parser.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="fällige Beiträge veröffentlichen (für Cron)")
    run.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts veröffentlichen")
    sub.add_parser("list", help="Zeitplan und Status anzeigen")
    sub.add_parser("check", help="CSV und Medien-URLs prüfen")
    retry = sub.add_parser("retry", help="fehlgeschlagenen Beitrag erneut einplanen")
    retry.add_argument("id")
    args = parser.parse_args(argv)

    load_dotenv(args.env)
    cfg = Config.from_env(args.env.resolve().parent)

    if args.command == "run":
        return cmd_run(cfg, dry_run=args.dry_run)
    if args.command == "list":
        return cmd_list(cfg)
    if args.command == "check":
        return cmd_check(cfg)
    return cmd_retry(cfg, args.id)


if __name__ == "__main__":
    sys.exit(main())
