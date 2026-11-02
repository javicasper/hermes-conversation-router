# hermes-conversation-router

**Your Hermes chats never end. This plugin ends them without cutting you off mid-topic, and brings old topics back when you return to them.**

A [Hermes Agent](https://github.com/NousResearch/hermes-agent) plugin for the messaging gateway (Telegram, WhatsApp, Signal, email, Discord, Slack, Matrix…). Model- and provider-agnostic: it uses whatever auxiliary model you already run.

## The problem

Since v2026.9.11, Hermes only starts a new gateway conversation when you type `/new`. Nobody does that in a chat app, so one conversation grows for days and **every reply re-sends the whole history**: slower, more expensive, and the model gets confused by stale context.

The existing fixes only look at the clock:

| | Decides by | Resumes old topics | Across platforms |
|---|---|---|---|
| `hermes-session-reset-policy` | idle time / time of day | no | no |
| `session-relay` | context size (auto only on desktop) | no | no |
| **conversation-router** | **what you actually wrote** | **yes, with a summary** | **yes, per person** |

## How it works

While a chat is alive, nothing happens — no extra calls. When someone writes again after a pause (40 min by default), one small call to a cheap auxiliary model picks one of:

- **continue**: same matter (“yes”, “and that?”, a follow-up) → stays in the current conversation.
- **new**: a different matter → fresh conversation, same code path as `/new` (memory and skills of the old one are saved as usual).
- **resume**: it clearly matches a recent conversation → fresh conversation with a **short summary** of that one prepended. Never the full history; the agent can still dig with `session_search`.

Two safety valves turn “continue” into “resume with a summary of the current one”: crossing the daily boundary (05:00 by default) and a prompt over 100k tokens.

```
you (after 3 h): "did the backup script end up working?"
router:          resume → «Nightly backup script» (yesterday 18:32)
chat:            🔀 Resuming «Nightly backup script»
agent gets:      [Context: resuming … Summary: wrote backup.sh, cron at 03:00 … pending: test restore]
                 did the backup script end up working?
```

Summaries are written in the background when a conversation closes and cached, so resuming waits for nobody. If anything fails (model down, bad JSON, schema change) the message just continues in the current conversation.

### Per person, across platforms

Each person only ever resumes **their own** conversations. Group your identities under one name and you can start something on Telegram and pick it up on WhatsApp; someone else in the same household never gets your topics (or you theirs). Anyone not listed only resumes their own conversations on the same platform. Group chats are skipped by default.

## Install

```bash
hermes plugins install javicasper/hermes-conversation-router --enable
hermes gateway restart        # or restart your gateway service
```

Requires Hermes ≥ v2026.9.11 (tested on v0.21.6, 2026.9.24). It uses a few gateway internals (`_handle_reset_command`, `_session_key_for_source`, `_delivery_adapter_for`); if a future release renames them, the router fails safe (messages simply continue) and logs a warning.

## Configure (optional)

Everything has a sensible default. Settings live in `~/.hermes/config.yaml`:

```yaml
plugins:
  entries:
    conversation-router:
      settings:
        idle_minutes: 40            # pause before the router even looks
        day_boundary_hour: 5        # null to disable
        large_context_tokens: 100000
        lookback_days: 7            # how far back "resume" can reach
        max_candidates: 8
        dm_only: true
        platforms: [telegram, whatsapp, email]
        notify_platforms: [telegram, whatsapp]   # where to post the 🆕/🔀 notice
        people:                     # optional: group identities per person
          Alice: ["telegram:123456789", "whatsapp:34600111222", "email:alice@example.org"]
          Bob:   ["telegram:987654321"]
        messages:                   # notice texts, any language; empty string = silent
          new: "🆕 Nueva conversación"
          resume: "🔀 Retomo «{title}»"
        log_preview_chars: 200      # 0 = don't store message text in the decision log
```

User ids are the ones Hermes stores in `state.db` (`sessions.user_id`); for WhatsApp the `@s.whatsapp.net` suffix is ignored.

### Which model?

The router registers its own auxiliary task, `conversation_router`, which **inherits from `title_generation`** — whatever small model you already use for titles. Override it like any auxiliary task:

```yaml
auxiliary:
  conversation_router:
    provider: openrouter          # any provider Hermes supports
    model: some/small-model
```

A small, fast model is plenty: the decision is a ~60-token JSON answer.

## Checking how it behaves

Every decision is appended to `~/.hermes/plugin-data/conversation-router/decisions.jsonl`:

```json
{"ts": "2026-11-02T10:53:48", "session": "…", "platform": "telegram", "pause_min": 611,
 "message": "…", "decision": "new", "sec": 4.1}
```

Summaries are cached in `summaries.json` next to it (pruned after twice the lookback window).

## Development

```bash
pip install pytest && pytest -q tests
```

## License

MIT
