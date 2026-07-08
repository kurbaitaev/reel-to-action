# reel-to-action

Send a link (Instagram reel, YouTube, TikTok, article) to your Telegram bot → get back actionable items: podcasts with real links, books, tools/Claude skills to try, concrete advice — personalized against your Obsidian vault and saved into `Action Inbox/` there.

**Architecture:** `bot.py` is a thin Telegram poller. For each link it:
1. **Acquires** the media in deterministic Python ([acquire.py](acquire.py)) — Instagram via an **Apify** actor (proxied, anti-bot), everything else via yt-dlp fallback.
2. **Reasons** by spawning a headless Claude Code agent (`claude -p`, instructions in [agent_prompt.md](agent_prompt.md)) with the transcript/caption already in hand: it transcribes the local video (Gemini), extracts typed items, finds real links via web search, checks relevance against your vault, and writes the note.
3. Replies with a digest and records the result in [ledger.json](ledger.json) so the same link isn't reprocessed (send a message containing `/force` to redo).

Agent permissions are scoped in [.claude/settings.json](.claude/settings.json). Keeping acquisition out of the agent makes it cheaper, more reliable, and ready to lift into a hosted backend.

## Setup (5 min)

0. **Log in the Claude CLI** (currently it returns 401 in headless mode): open a terminal, run `claude`, then `/login`. Verify with:
   ```bash
   claude -p "Reply with exactly: OK"
   ```
1. **Create the bot**: message [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
2. **Configure**:
   ```bash
   cp .env.example .env   # paste the token
   ```
3. **Run**:
   ```bash
   python3 bot.py
   ```
4. Send `/start` to your bot — it replies with your user id. Put that id into `ALLOWED_USER_IDS` in `.env` and restart. **Don't skip this** — until then anyone who discovers the bot can trigger agent runs on your Mac.
5. Send it a reel link. First reply takes 2–5 min.

To test the pipeline without Telegram:
```bash
python3 bot.py --test "https://www.instagram.com/reel/..."
```

## Keeping it running (launchd — installed)

The bot runs as a launchd **LaunchAgent** (`~/Library/LaunchAgents/com.kurbaitaev.reel-to-action.plist`): it starts on login and auto-restarts if it crashes. The Mac still has to be powered on and logged in (a LaunchAgent only runs in your GUI session — that's required so it can read your Claude credentials from the login Keychain).

Manage it with `./ctl.sh`:
```bash
./ctl.sh status     # is it running? pid? last exit code
./ctl.sh restart    # after editing bot.py / agent_prompt.md
./ctl.sh stop
./ctl.sh start
./ctl.sh logs       # dump logs
./ctl.sh tail       # follow logs live
```

**Auto-recovery:** while a reel is processing it's recorded in `pending.json`. If the bot is killed mid-reel (restart, Mac sleep, network drop), the entry survives and the bot **re-processes it automatically on next startup** — reels are no longer silently dropped. Transient network errors are caught by a registered handler and retried rather than crashing.

**Heads-up — Claude token expiry:** the agent uses your Claude Code OAuth login, which expires every ~few months. When digests start failing with a 401, run `claude` → `/login` once and it resumes (no need to touch the service). To avoid this entirely, set `ANTHROPIC_API_KEY` and the agent will use that instead (bills per-token rather than via your Max plan).

## Notes & current limits

- **Cost**: each link is one headless Claude Code run on your existing Claude login.
- **Transcription**: prefers your `gemini-analyze` MCP; falls back to direct Gemini API if `GEMINI_API_KEY` is set in `.env`.
- **Structured output & formatting**: the agent emits a single `@@JSON@@…@@END@@` object (`summary`, `note`, `items[]`). `bot.py` renders the Telegram message from it as **HTML** — bold section headers, item titles hyperlinked (so raw URLs never show), special chars escaped — and `notion.py` writes one row per item to the Notion database (`NOTION_TOKEN` + `NOTION_DATABASE_ID` in `.env`; skipped silently if unset). Database columns: Name / Type / Status / Source / Link / Author / Why it matters / Date.
- **Calendar booking**: v1 *suggests* time blocks in the digest; auto-booking via Calendar MCP has the same headless-auth caveat as Notion.
- **Storage is local, not iCloud.** Action items are written to `reel-to-action/vault/Action Inbox/` (inside this project, on the laptop). The iCloud-synced `~/Documents/Obsidian Vault` is used **read-only** for personalization context — the bot never writes there. Paths are in `agent_prompt.md`; to browse the output in Obsidian, open `reel-to-action/vault/` as a vault.
