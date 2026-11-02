"""Conversation router for Hermes Agent.

Hermes never closes a messaging conversation on its own (since v2026.9.11 only /new does), so a
Telegram / WhatsApp / Signal / email chat grows forever and every model call drags the whole
history along. Time-based resets (hermes-session-reset-policy) cut blindly: they also cut you off
in the middle of a topic and never bring an old one back.

This plugin decides, only when someone writes again after a pause, what to do with the new message:

- continue: it belongs to the current conversation; nothing is touched.
- new:      different topic; a fresh conversation starts (same code path as /new).
- resume:   it belongs to a recent conversation (or to the current one, but after the daily
            boundary or with a very large context); a fresh conversation starts and a SHORT
            SUMMARY of that one is prepended to the message, never the full history.

Cost: while a chat is alive there are no extra calls. After a pause there is one small call to an
auxiliary model (task ``conversation_router``, which by default inherits whatever you configured
for ``title_generation``). Summaries are written in the background when a conversation closes and
cached on disk, so resuming waits for nobody.

Privacy: each person only ever resumes THEIR conversations. Identities across platforms can be
grouped under one person (``people`` setting); anyone not listed only resumes their own
conversations on the same platform. Group chats are skipped by default.

If anything fails (model down, odd JSON, schema change) the message simply goes on in the current
conversation. Every decision is appended to ``decisions.jsonl`` so you can check how it behaves.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("hermes.plugins.conversation_router")

PLUGIN_NAME = "conversation-router"
TASK = "conversation_router"
CLOCK_KEY = "conversation_router_last_message"

DEFAULTS: dict[str, Any] = {
    "idle_minutes": 40,             # below this the router does not even ask: same chat
    "day_boundary_hour": 5,         # after this hour, yesterday's history is not dragged along (null = off)
    "large_context_tokens": 100_000,  # past this prompt size, "continue" becomes "resume with summary"
    "lookback_days": 7,             # only conversations from the last N days can be resumed
    "max_candidates": 8,
    "platforms": ["telegram", "whatsapp", "signal", "email", "discord", "slack", "matrix",
                  "mattermost", "sms", "bluebubbles", "weixin"],
    "notify_platforms": ["telegram", "whatsapp", "signal", "discord", "slack", "matrix",
                         "mattermost", "bluebubbles", "weixin"],  # not email: a notice would be an extra mail
    "dm_only": True,
    "people": {},                   # {"Alice": ["telegram:123", "whatsapp:34600111222", "email:a@x.org"]}
    "messages": {
        "new": "🆕 New conversation",
        "resume": "🔀 Resuming «{title}»",
    },
    "log_preview_chars": 200,       # 0 = do not store message text in decisions.jsonl
}

_ctx: Any = None
_lock = threading.Lock()


# --- Settings ---------------------------------------------------------------------------------
def _cfg(key: str) -> Any:
    default = DEFAULTS[key]
    if _ctx is None:
        return default
    try:
        value = _ctx.get_config(key, default)
    except Exception:
        return default
    if isinstance(default, dict) and isinstance(value, dict):
        return {**default, **value}
    return default if value is None and key != "day_boundary_hour" else value


def _hermes_home() -> Path:
    try:
        from hermes_constants import get_hermes_home
        return Path(get_hermes_home())
    except Exception:
        return Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes")


def _data_dir() -> Path:
    try:
        path = Path(_ctx.state.data_dir)
    except Exception:
        path = _hermes_home() / "plugin-data" / PLUGIN_NAME
    path.mkdir(parents=True, exist_ok=True)
    return path


# --- Identities ---------------------------------------------------------------------------------
def _norm(platform: str, user_id: Any) -> tuple[str, str]:
    platform = str(platform or "").strip().lower()
    uid = str(user_id or "").strip().lower()
    if platform == "whatsapp":          # 34600111222@s.whatsapp.net -> 34600111222
        uid = uid.split("@", 1)[0]
    return platform, uid


def _people() -> dict[str, set[tuple[str, str]]]:
    out: dict[str, set[tuple[str, str]]] = {}
    for name, ids in (_cfg("people") or {}).items():
        group = set()
        for item in ids or []:
            if isinstance(item, str) and ":" in item:
                platform, uid = item.split(":", 1)
                group.add(_norm(platform, uid))
        if group:
            out[str(name)] = group
    return out


def person_of(platform: str, user_id: Any, people: Optional[dict] = None) -> Optional[str]:
    key = _norm(platform, user_id)
    people = _people() if people is None else people
    return next((name for name, ids in people.items() if key in ids), None)


def identities_of(platform: str, user_id: Any, people: Optional[dict] = None) -> set[tuple[str, str]]:
    """Every identity of the same person, or just this one if the person is not configured."""
    people = _people() if people is None else people
    name = person_of(platform, user_id, people)
    return set(people[name]) if name else {_norm(platform, user_id)}


# --- Summary store --------------------------------------------------------------------------------
def _summaries_path() -> Path:
    return _data_dir() / "summaries.json"


def _load_summaries() -> dict[str, dict]:
    try:
        return json.loads(_summaries_path().read_text())
    except Exception:
        return {}


def _save_summary(session_id: str, data: dict) -> None:
    with _lock:
        all_ = _load_summaries()
        all_[session_id] = data
        cutoff = time.time() - int(_cfg("lookback_days")) * 2 * 86400
        all_ = {k: v for k, v in all_.items() if v.get("end", 0) >= cutoff}
        path = _summaries_path()
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(all_, ensure_ascii=False, indent=1))
        tmp.replace(path)


def _log_decision(record: dict) -> None:
    try:
        with (_data_dir() / "decisions.jsonl").open("a") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        logger.debug("conversation-router: could not log decision", exc_info=True)


# --- state.db (read only) -------------------------------------------------------------------------
def _db() -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{_hermes_home() / 'state.db'}?mode=ro", uri=True, timeout=5)
    con.row_factory = sqlite3.Row
    return con


_NOISE = ("Gateway message origin", "[IMPORTANT: Background process", "[Webhook delivery")


def _session_info(session_id: str) -> dict:
    with _db() as con:
        row = con.execute(
            "select id, title, source, user_id, started_at, "
            "coalesce(last_activity_at, ended_at, started_at) as end_ts from sessions where id=?",
            (session_id,)).fetchone()
    return dict(row) if row else {}


def _transcript(session_id: str, max_chars: int, per_message: int = 600) -> str:
    """User/assistant text of a session (no tool traffic), trimmed from the start."""
    info = _session_info(session_id)
    speaker = person_of(info.get("source") or "", info.get("user_id")) or "User"
    with _db() as con:
        rows = con.execute(
            "select role, content from messages where session_id=? and role in ('user','assistant') "
            "and content is not null and content != '' order by timestamp, id", (session_id,)).fetchall()
    lines, seen = [], set()
    for row in rows:
        text = str(row["content"]).strip()
        if not text or text.startswith(_NOISE) or text in seen:
            continue
        seen.add(text)
        lines.append(f"{speaker if row['role'] == 'user' else 'Assistant'}: {text[:per_message]}")
    return "\n".join(lines)[-max_chars:]


def _recent(exclude: str, identities: set[tuple[str, str]]) -> list[dict]:
    """Recent conversations of THIS person, on any of their platforms."""
    platforms = sorted({p for p, _ in identities} & set(_cfg("platforms")))
    if not platforms:
        return []
    since = time.time() - int(_cfg("lookback_days")) * 86400
    marks = ",".join("?" * len(platforms))
    with _db() as con:
        rows = con.execute(
            "select id, title, source, user_id, coalesce(last_activity_at, ended_at, started_at) as end_ts "
            f"from sessions where source in ({marks}) and id != ? and message_count > 2 "
            "and coalesce(last_activity_at, ended_at, started_at) >= ? order by end_ts desc",
            (*platforms, exclude, since)).fetchall()
    own = [dict(r) for r in rows if _norm(r["source"], r["user_id"]) in identities]
    return own[:int(_cfg("max_candidates"))]


# --- Auxiliary model calls ------------------------------------------------------------------------
def parse_json(text: str) -> Optional[dict]:
    text = (text or "").strip()
    if "{" in text and "}" in text:
        text = text[text.index("{"): text.rindex("}") + 1]
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except Exception:
        return None


SUMMARY_PROMPT = """Summarise this conversation between a user and their assistant so it can be resumed another day without the history.
Reply ONLY with JSON: {"title": "...", "topic": "one line", "summary": "..."}
- Write title, topic and summary in the same language the conversation uses.
- "summary": at most 150 words: what was done, the state it was left in, what is pending, and the concrete data that matters (paths, names, commands, decisions). No filler."""

DECISION_PROMPT = """You are the conversation router of a personal assistant on chat apps. The user writes again after a pause.
Decide what to do with the new message:
- "continue": it follows the current conversation (short replies, "yes", "and that?", follow-ups on the same matter).
- "resume": it clearly matches one of the recent conversations listed (give its "id").
- "new": it is a different matter.
When unsure between "continue" and "new", choose "continue". Use "resume" only for a clear match.
Reply ONLY with JSON: {"decision": "continue|new|resume", "id": "id or null"}"""


def _summarise_sync(session_id: str) -> Optional[dict]:
    if _ctx is None:
        return None
    info = _session_info(session_id)
    text = _transcript(session_id, max_chars=14_000)
    if not text:
        return None
    reply = _ctx.llm.complete(
        messages=[{"role": "system", "content": SUMMARY_PROMPT}, {"role": "user", "content": text}],
        max_tokens=600, task=TASK, purpose="conversation_router.summary")
    data = parse_json(reply.text)
    if not data or not data.get("summary"):
        return None
    data.update(title=data.get("title") or info.get("title") or "", end=info.get("end_ts") or time.time())
    _save_summary(session_id, data)
    return data


def _summarise_in_background(session_id: str) -> None:
    if not session_id or session_id in _load_summaries():
        return

    def work():
        try:
            if _session_info(session_id).get("source") in set(_cfg("platforms")):
                _summarise_sync(session_id)
        except Exception:
            logger.warning("conversation-router: could not summarise %s", session_id, exc_info=True)

    threading.Thread(target=work, name="conversation-router-summary", daemon=True).start()


async def _decide(text: str, current_id: str, candidates: list[dict], summaries: dict) -> Optional[dict]:
    info = _session_info(current_id)
    current = _transcript(current_id, max_chars=1_500, per_message=300)
    listing = "\n".join(
        f'- id={c["id"]} ({c.get("source")}) | '
        f'{c.get("title") or summaries.get(c["id"], {}).get("title") or "(untitled)"}'
        + (f' | {summaries[c["id"]]["topic"]}' if summaries.get(c["id"], {}).get("topic") else "")
        for c in candidates) or "(none)"
    prompt = (f"CURRENT CONVERSATION: {info.get('title') or '(untitled)'}\nLast messages:\n{current or '(empty)'}\n\n"
              f"RECENT CONVERSATIONS:\n{listing}\n\nNEW MESSAGE:\n{text[:1500]}")
    reply = await _ctx.llm.acomplete(
        messages=[{"role": "system", "content": DECISION_PROMPT}, {"role": "user", "content": prompt}],
        max_tokens=60, task=TASK, purpose="conversation_router.decision")
    data = parse_json(reply.text)
    if not data or data.get("decision") not in ("continue", "new", "resume"):
        return None
    if data["decision"] == "resume" and data.get("id") not in {c["id"] for c in candidates}:
        data["decision"] = "new"
    return data


# --- Gateway hooks --------------------------------------------------------------------------------
def day_boundary(now: datetime, hour: int) -> datetime:
    edge = now.replace(hour=hour, minute=0, second=0, microsecond=0)
    return edge if edge <= now else edge - timedelta(days=1)


def _last_message(entry: Any) -> Optional[datetime]:
    value = (getattr(entry, "metadata", None) or {}).get(CLOCK_KEY)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            pass
    return getattr(entry, "updated_at", None) or getattr(entry, "created_at", None)


def _platform_of(source: Any) -> str:
    return str(getattr(getattr(source, "platform", None), "value", "") or "")


async def _start_fresh(gateway: Any, event: Any) -> None:
    """New conversation through the same path as /new (empty text: no title)."""
    reset = getattr(gateway, "_handle_reset_command", None)
    if not callable(reset):
        raise RuntimeError("/new code path not available in this Hermes version")
    await reset(dataclasses.replace(event, text=""))


async def _notify(gateway: Any, source: Any, text: str) -> None:
    if not text or _platform_of(source) not in set(_cfg("notify_platforms")):
        return
    try:
        adapter = gateway._delivery_adapter_for(source)
        if adapter is not None:
            await adapter.send(source.chat_id, text)
    except Exception:
        logger.debug("conversation-router: notice not sent", exc_info=True)


async def on_message(event: Any, gateway: Any, session_store: Any, **_: Any) -> Optional[dict]:
    if session_store is None or _ctx is None:
        return None
    source = getattr(event, "source", None)
    platform = _platform_of(source)
    if source is None or platform not in set(_cfg("platforms")):
        return None
    if _cfg("dm_only") and str(getattr(source, "chat_type", "dm") or "dm") != "dm":
        return None
    text = (getattr(event, "text", None) or "").strip()
    if text.startswith("/"):
        return None
    try:
        key = gateway._session_key_for_source(source)
        entry = session_store.lookup_by_session_key(key) if key else None
    except Exception:
        return None
    if entry is None:
        return None

    now = datetime.now()
    last = _last_message(entry)
    mark = lambda k: session_store.set_session_metadata(k, CLOCK_KEY, now.isoformat())  # noqa: E731

    if (last is None or now - last < timedelta(minutes=int(_cfg("idle_minutes")))
            or getattr(entry, "active_turn_token", None)):
        mark(key)
        return None

    current_id = entry.session_id
    hour = _cfg("day_boundary_hour")
    crossed_boundary = hour is not None and last < day_boundary(now, int(hour))
    large_context = (getattr(entry, "last_prompt_tokens", 0) or 0) >= int(_cfg("large_context_tokens"))
    preview = int(_cfg("log_preview_chars") or 0)
    record = {"ts": now.isoformat(timespec="seconds"), "session": current_id, "platform": platform,
              "pause_min": round((now - last).total_seconds() / 60)}
    if preview:
        record["message"] = text[:preview]
    t0 = time.monotonic()
    try:
        if not text:
            # Voice note or media not transcribed yet: only the time rule applies.
            decision = {"decision": "new" if crossed_boundary else "continue", "reason": "no-text"}
        else:
            summaries = _load_summaries()
            user_id = getattr(source, "user_id", None)
            if person_of(platform, user_id) is None:   # e.g. a WhatsApp @lid: trust the stored session owner
                user_id = _session_info(current_id).get("user_id") or user_id
            candidates = _recent(current_id, identities_of(platform, user_id))
            decision = await _decide(text, current_id, candidates, summaries)
            if decision is None:
                mark(key)
                record.update(decision="error")
                return None
            if decision["decision"] == "continue" and (crossed_boundary or large_context):
                # Same matter, but not worth dragging the whole history along.
                decision = {"decision": "resume", "id": current_id,
                            "reason": "day-boundary" if crossed_boundary else "large-context"}

        record.update(decision)
        if decision["decision"] == "continue":
            mark(key)
            return None

        rewritten = None
        if decision["decision"] == "resume":
            target = decision["id"]
            summary = _load_summaries().get(target) or await asyncio.to_thread(_summarise_sync, target)
            if summary:
                when = datetime.fromtimestamp(summary.get("end") or time.time()).strftime("%Y-%m-%d %H:%M")
                rewritten = (f"[Context: resuming the conversation «{summary['title']}» ({when}). Summary:\n"
                             f"{summary['summary']}\nIf you need more detail, look it up with session_search.]"
                             f"\n\n{text}")
            else:
                decision["decision"] = record["decision"] = "new"

        await _start_fresh(gateway, event)
        mark(gateway._session_key_for_source(source))
        messages = _cfg("messages")
        if rewritten:
            title = _load_summaries().get(decision["id"], {}).get("title", "")
            await _notify(gateway, source, str(messages.get("resume", "")).replace("{title}", title))
        else:
            await _notify(gateway, source, str(messages.get("new", "")))
        return {"action": "rewrite", "text": rewritten} if rewritten else None
    except Exception as exc:
        logger.warning("conversation-router: failed, staying in the current conversation: %s", exc, exc_info=True)
        record.update(decision="error", error=str(exc)[:200])
        try:
            mark(key)
        except Exception:
            pass
        return None
    finally:
        record["sec"] = round(time.monotonic() - t0, 1)
        _log_decision(record)


def on_session_finalize(session_id: Any = None, old_session_id: Any = None, **_: Any) -> None:
    _summarise_in_background(str(old_session_id or session_id or ""))


def register(ctx: Any) -> None:
    global _ctx
    _ctx = ctx
    ctx.register_auxiliary_task(
        TASK, display_name="Conversation router",
        description="Decides whether a message continues the chat, starts a new one or resumes a recent one; "
                    "summarises conversations as they close.",
        inherit_from="title_generation")
    ctx.register_hook("pre_gateway_dispatch", on_message)
    ctx.register_hook("on_session_finalize", on_session_finalize)
    logger.info("conversation-router armed (idle %s min, day boundary %s)",
                _cfg("idle_minutes"), _cfg("day_boundary_hour"))
