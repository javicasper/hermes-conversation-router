"""Unit tests for the pure parts of the router (no Hermes needed)."""
import asyncio
import importlib.util
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("conversation_router", ROOT / "__init__.py")
router = importlib.util.module_from_spec(spec)
sys.modules["conversation_router"] = router
spec.loader.exec_module(router)


class FakeCtx:
    def __init__(self, settings, data_dir, replies=None):
        self.settings, self.replies = settings, list(replies or [])
        self.state = SimpleNamespace(data_dir=data_dir)
        self.llm = self

    def get_config(self, key, default=None):
        return self.settings.get(key, default)

    async def acomplete(self, **_):
        return SimpleNamespace(text=self.replies.pop(0))

    def complete(self, **_):
        return SimpleNamespace(text=self.replies.pop(0))


PEOPLE = {"Alice": ["telegram:111", "whatsapp:34600111222", "email:Alice@Example.org"],
          "Bob": ["telegram:222"]}


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(router, "_hermes_home", lambda: tmp_path)
    con = sqlite3.connect(tmp_path / "state.db")
    con.executescript("""
        create table sessions (id text, title text, source text, user_id text, started_at real,
                               ended_at real, last_activity_at real, message_count int);
        create table messages (id integer primary key, session_id text, role text, content text, timestamp real);
    """)
    now = time.time()
    rows = [("a1", "Media server", "telegram", "111", now - 3600),
            ("a2", "Mail setup", "whatsapp", "34600111222@s.whatsapp.net", now - 7200),
            ("b1", "Bob's secret", "telegram", "222", now - 600),
            ("x1", "Stranger", "telegram", "999", now - 600),
            ("old", "Ancient", "telegram", "111", now - 30 * 86400)]
    con.executemany("insert into sessions values (?,?,?,?,?,null,?,5)", [(i, t, s, u, ts, ts) for i, t, s, u, ts in rows])
    con.commit()
    con.close()
    router._ctx = FakeCtx({"people": PEOPLE}, tmp_path / "data")
    yield tmp_path
    router._ctx = None


def test_identity_grouping(home):
    assert router.person_of("whatsapp", "34600111222@s.whatsapp.net") == "Alice"
    assert router.person_of("email", "alice@example.org") == "Alice"
    assert router.person_of("telegram", "999") is None
    assert router.identities_of("telegram", "999") == {("telegram", "999")}


def test_candidates_never_cross_people(home):
    alice = {c["id"] for c in router._recent("none", router.identities_of("telegram", "111"))}
    assert alice == {"a1", "a2"}                      # both of her platforms, nothing older than lookback
    bob = {c["id"] for c in router._recent("none", router.identities_of("telegram", "222"))}
    assert bob == {"b1"}
    stranger = {c["id"] for c in router._recent("none", router.identities_of("telegram", "999"))}
    assert stranger == {"x1"}


def test_parse_json():
    assert router.parse_json('blah {"decision": "new", "id": null} blah') == {"decision": "new", "id": None}
    assert router.parse_json("nope") is None


def test_day_boundary():
    assert router.day_boundary(datetime(2026, 1, 2, 3, 0), 5) == datetime(2026, 1, 1, 5, 0)
    assert router.day_boundary(datetime(2026, 1, 2, 9, 0), 5) == datetime(2026, 1, 2, 5, 0)


def test_resume_rejects_foreign_id(home):
    router._ctx.replies = ['{"decision": "resume", "id": "b1"}']
    candidates = router._recent("a1", router.identities_of("telegram", "111"))
    decision = asyncio.run(router._decide("hi", "a1", candidates, {}))
    assert decision["decision"] == "new"


def test_settings_merge(home):
    router._ctx.settings["messages"] = {"new": "Nueva"}
    msgs = router._cfg("messages")
    assert msgs["new"] == "Nueva" and "{title}" in msgs["resume"]
    router._ctx.settings["day_boundary_hour"] = None
    assert router._cfg("day_boundary_hour") is None
