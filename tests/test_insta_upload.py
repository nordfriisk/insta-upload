from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import insta_upload as iu

TZ = ZoneInfo("Europe/Berlin")


class FakeResponse:
    def __init__(self, body, status_code=200):
        self.body = body
        self.status_code = status_code
        self.text = str(body)

    def json(self):
        return self.body


class FakeSession:
    """Simuliert die Graph API und merkt sich alle Aufrufe."""

    def __init__(self, statuses=None, fail_on=None):
        self.calls = []
        self.statuses = statuses or {}
        self.fail_on = fail_on
        self.counter = 0

    def post(self, url, data, timeout):
        self.calls.append(("POST", url, dict(data)))
        if self.fail_on and self.fail_on in url:
            return FakeResponse({"error": {"type": "OAuthException", "code": 190, "message": "Token abgelaufen"}}, 400)
        self.counter += 1
        return FakeResponse({"id": f"id{self.counter}"})

    def get(self, url, params, timeout):
        self.calls.append(("GET", url, dict(params)))
        container = url.rsplit("/", 1)[1]
        seq = self.statuses.get(container, ["FINISHED"])
        return FakeResponse({"status_code": seq.pop(0) if len(seq) > 1 else seq[0]})

    def posts(self):
        return [c for c in self.calls if c[0] == "POST"]


@pytest.fixture
def cfg(tmp_path):
    return iu.Config(
        ig_user_id="1784",
        access_token="TOKEN",
        media_base_url="https://example.infomaniak.ch/insta/",
        schedule_file=tmp_path / "posts.csv",
        state_db=tmp_path / "state.db",
        timezone=TZ,
        poll_interval=1,
        poll_timeout=3,
    )


def write_csv(path: Path, rows: list[str]):
    path.write_text("id,publish_at,type,media,caption\n" + "\n".join(rows) + "\n", encoding="utf-8")


def make_post(type_="post", media=("a.jpg",), caption="Hallo"):
    return iu.ScheduledPost(id="x", publish_at=datetime.now(timezone.utc), type=type_, media=list(media), caption=caption)


def client_for(cfg, session):
    return iu.GraphClient(cfg, session=session, sleep=lambda s: None)


# --- CSV ---------------------------------------------------------------------

def test_read_schedule_parses_rows_and_timezone(cfg):
    write_csv(cfg.schedule_file, [
        '1,2026-10-20 18:00,post,a.jpg|b.jpg,"Sonnenuntergang, Sylt"',
        "2,2026-10-21T08:00:00+00:00,story,s.mp4,",
    ])
    posts = iu.read_schedule(cfg.schedule_file, TZ)
    assert [p.errors for p in posts] == [[], []]
    assert posts[0].media == ["a.jpg", "b.jpg"]
    assert posts[0].is_carousel
    assert posts[0].caption == "Sonnenuntergang, Sylt"
    assert posts[0].publish_at.utcoffset() == timedelta(hours=2)  # Sommerzeit Berlin
    assert posts[1].publish_at.tzinfo == timezone.utc


@pytest.mark.parametrize("row, message", [
    ("1,morgen,post,a.jpg,", "ungültiges Datum"),
    ("1,2026-10-20 18:00,video,a.jpg,", "unbekannter Typ"),
    ("1,2026-10-20 18:00,reel,a.jpg,", "braucht ein Video"),
    ("1,2026-10-20 18:00,story,a.jpg|b.jpg,", "nur eine Datei"),
    ("1,2026-10-20 18:00,post,a.gif,", "nicht unterstütztes Format"),
    (",2026-10-20 18:00,post,a.jpg,", "'id' ist leer"),
])
def test_read_schedule_reports_invalid_rows(cfg, row, message):
    write_csv(cfg.schedule_file, [row])
    (post,) = iu.read_schedule(cfg.schedule_file, TZ)
    assert any(message in e for e in post.errors), post.errors


def test_duplicate_ids_are_rejected(cfg):
    write_csv(cfg.schedule_file, ["1,2026-10-20 18:00,post,a.jpg,", "1,2026-10-21 18:00,post,b.jpg,"])
    posts = iu.read_schedule(cfg.schedule_file, TZ)
    assert posts[0].errors == []
    assert "doppelt" in posts[1].errors[0]


def test_media_url():
    base = "https://example.ch/insta/"
    assert iu.media_url(base, "a.jpg") == "https://example.ch/insta/a.jpg"
    assert iu.media_url(base, "/sub/a.jpg") == "https://example.ch/insta/sub/a.jpg"
    assert iu.media_url(base, "https://other.ch/b.jpg") == "https://other.ch/b.jpg"


# --- Graph API ---------------------------------------------------------------

def test_publish_single_image(cfg):
    session = FakeSession()
    media_id = iu.publish_post(client_for(cfg, session), make_post(), cfg.media_base_url)
    create, publish = session.posts()
    assert create[1].endswith("/v24.0/1784/media")
    assert create[2] == {"image_url": "https://example.infomaniak.ch/insta/a.jpg", "caption": "Hallo", "access_token": "TOKEN"}
    assert publish[1].endswith("/1784/media_publish")
    assert publish[2]["creation_id"] == "id1"
    assert media_id == "id2"


def test_publish_story_video(cfg):
    session = FakeSession()
    iu.publish_post(client_for(cfg, session), make_post("story", ["s.mp4"]), cfg.media_base_url)
    create = session.posts()[0][2]
    assert create["media_type"] == "STORIES"
    assert create["video_url"].endswith("/s.mp4")
    assert "caption" not in create


def test_single_video_post_becomes_reel(cfg):
    session = FakeSession()
    iu.publish_post(client_for(cfg, session), make_post("post", ["v.mov"]), cfg.media_base_url)
    create = session.posts()[0][2]
    assert create["media_type"] == "REELS"
    assert create["share_to_feed"] == "true"


def test_publish_carousel(cfg):
    session = FakeSession()
    iu.publish_post(client_for(cfg, session), make_post("post", ["a.jpg", "b.mp4"]), cfg.media_base_url)
    child1, child2, parent, publish = session.posts()
    assert child1[2]["is_carousel_item"] == "true" and "media_type" not in child1[2]
    assert child2[2]["media_type"] == "VIDEO"
    assert parent[2]["media_type"] == "CAROUSEL"
    assert parent[2]["children"] == "id1,id2"
    assert publish[2]["creation_id"] == "id3"


def test_waits_for_video_processing(cfg):
    session = FakeSession(statuses={"id1": ["IN_PROGRESS", "IN_PROGRESS", "FINISHED"]})
    iu.publish_post(client_for(cfg, session), make_post("reel", ["r.mp4"]), cfg.media_base_url)
    assert len([c for c in session.calls if c[0] == "GET"]) == 3


def test_processing_error_raises(cfg):
    session = FakeSession(statuses={"id1": ["ERROR"]})
    with pytest.raises(iu.GraphError, match="ERROR"):
        iu.publish_post(client_for(cfg, session), make_post("reel", ["r.mp4"]), cfg.media_base_url)


def test_processing_timeout_raises(cfg):
    session = FakeSession(statuses={"id1": ["IN_PROGRESS"]})
    with pytest.raises(iu.GraphError, match="noch nicht fertig"):
        iu.publish_post(client_for(cfg, session), make_post("reel", ["r.mp4"]), cfg.media_base_url)


# --- run ---------------------------------------------------------------------

def test_run_publishes_only_due_posts_once(cfg):
    past = (datetime.now(TZ) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    future = (datetime.now(TZ) + timedelta(days=1)).strftime("%Y-%m-%d %H:%M")
    write_csv(cfg.schedule_file, [f"due,{past},post,a.jpg,", f"later,{future},post,b.jpg,"])
    session = FakeSession()

    assert iu.cmd_run(cfg, client=client_for(cfg, session)) == 0
    assert len(session.posts()) == 2  # Container + Publish für 'due'
    row = iu.State(cfg.state_db).get("due")
    assert row["status"] == "published" and row["media_id"] == "id2"
    assert iu.State(cfg.state_db).get("later") is None

    iu.cmd_run(cfg, client=client_for(cfg, session))
    assert len(session.posts()) == 2  # nichts doppelt veröffentlicht


def test_run_retries_failures_up_to_max_attempts(cfg):
    past = (datetime.now(TZ) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    write_csv(cfg.schedule_file, [f"bad,{past},post,a.jpg,"])
    session = FakeSession(fail_on="/media")

    for _ in range(5):
        iu.cmd_run(cfg, client=client_for(cfg, session))
    row = iu.State(cfg.state_db).get("bad")
    assert row["status"] == "failed"
    assert row["attempts"] == cfg.max_attempts
    assert "Token abgelaufen" in row["last_error"]

    assert iu.cmd_retry(cfg, "bad") == 0
    assert iu.State(cfg.state_db).get("bad") is None


def test_dry_run_publishes_nothing(cfg):
    past = (datetime.now(TZ) - timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M")
    write_csv(cfg.schedule_file, [f"due,{past},post,a.jpg,"])
    session = FakeSession()
    iu.cmd_run(cfg, dry_run=True, client=client_for(cfg, session))
    assert session.calls == []
    assert iu.State(cfg.state_db).get("due") is None


def test_dotenv_does_not_override_environment(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('# Kommentar\nIG_USER_ID="123"\nTIMEZONE=UTC\n')
    monkeypatch.setenv("IG_USER_ID", "999")
    monkeypatch.delenv("TIMEZONE", raising=False)
    iu.load_dotenv(env)
    import os
    assert os.environ["IG_USER_ID"] == "999"
    assert os.environ["TIMEZONE"] == "UTC"
