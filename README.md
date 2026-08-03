# reel-to-action

Send an Instagram reel to your own Telegram bot. Get back a clean, permanent note — and keep it after the post disappears.

Saved reels are a graveyard. You bookmark something useful, and three weeks later you can't find it, can't remember which one had the book recommendation, and half of them have been deleted by the creator. This turns each one into a note you actually own: what it said, word for word, plus real links to every book, podcast, tool or concept it mentioned.

It reads the pictures too — a reel that shows a book cover on screen but never says the title still gets that book in the note, with a verified link.

Everything lands in a local markdown vault (Obsidian-compatible), optionally in a Notion database, and back in your Telegram chat.

> **Runs on your Mac, for you.** This is a single-user personal tool, not a hosted service. Your reels and notes never leave your machine except to the APIs you configure.

## What a note looks like

From a reel where an investor lists books he gives away:

> **Bill Gurley's 3 Most-Gifted Books**
>
> 📌 **Recommended**
> 1. ✅ [Complexity: The Emerging Science at the Edge of Order and Chaos](https://www.goodreads.com/book/show/337123.Complexity) — M. Mitchell Waldrop
> 2. ✅ [Mr. China](https://www.goodreads.com/book/show/109705.Mr_China) — Tim Clissold
> 3. ✅ [Range: Why Generalists Triumph in a Specialized World](https://www.goodreads.com/book/show/41795733-range) — David Epstein
> 4. ✅ [Runnin' Down a Dream: How to Thrive in a Career You Actually Love](https://www.goodreads.com/book/show/237134711-runnin-down-a-dream) — Bill Gurley
>
> 💾 *Curated reading list from a top venture capitalist…*
> 📄 Full transcript (collapsed)

The fourth book is never spoken aloud — it's on a cover shown on screen. ✅ means the link was checked; ⚠️ means it couldn't be confirmed, and the note says so rather than guessing.

## How it works

`bot.py` is a thin Telegram poller. Per link:

1. **Acquire** ([acquire.py](acquire.py)) — deterministic Python, no model involved. Downloads the media and metadata, and samples frames from the video with ffmpeg so on-screen text can be read. Carousels and photo posts download every image.
2. **Reason** — spawns a headless [Claude Code](https://claude.com/product/claude-code) agent (`claude -p`, instructions in [agent_prompt.md](agent_prompt.md)). It reads the transcript and every image, detects what kind of content it is, and web-searches to verify each link. It returns exactly one JSON object; it writes no files.
3. **Save** — `bot.py` renders the note and writes it to the vault, to Notion ([notion.py](notion.py)), and to Telegram. A ledger prevents reprocessing (send `/force` to redo).

Acquisition is kept out of the agent on purpose: it's the fragile, infrastructure-heavy part, and it belongs in code you can debug.

## Setup

**Requires macOS** (launchd for the service, Apple's ffmpeg build, Claude Code CLI) and **Python 3.10+**.

```bash
git clone https://github.com/kurbaitaev/reel-to-action.git
cd reel-to-action
pip install -r requirements.txt
brew install yt-dlp ffmpeg
cp .env.example .env
python3 doctor.py          # tells you exactly what's still missing
```

`doctor.py` is the fastest way to find out whether you're ready — it checks every dependency and prints the fix for each.

You need three things in `.env`:

1. **`TELEGRAM_BOT_TOKEN`** — message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token.
2. **Claude access** — either run `claude` once and `/login` (uses your Claude subscription), or set `ANTHROPIC_API_KEY` (bills per token, and never expires — see [Which to use](#claude-login-vs-api-key)).
3. **`ALLOWED_USER_IDS`** — start the bot, send it `/start`, and it replies with your numeric id. Put that in `.env` and restart. **The bot refuses everyone until you do this**, on purpose: an open bot lets anyone who finds the token run processes on your Mac.

Then:

```bash
python3 bot.py
```

Send it a reel. The first note takes 1–3 minutes.

To try the pipeline without Telegram:

```bash
python3 bot.py --test "https://www.instagram.com/reel/..."
```

### One-time trust step

The agent's permissions live in [.claude/settings.json](.claude/settings.json), and Claude Code ignores them in a directory you haven't trusted. Run `claude` once inside the project folder and accept the trust prompt — otherwise web search is silently disabled and links come back unverified. The watchdog re-checks this and repairs it if a future login resets it.

## Keeping it running

```bash
./install.sh        # installs two launchd services: the bot + a watchdog
./ctl.sh status     # running? pid? last exit code
./ctl.sh restart    # after editing bot.py or agent_prompt.md
./ctl.sh tail       # follow the log
```

The watchdog ([watchdog.py](watchdog.py)) checks every 90 minutes that the bot is alive *and* still polling, restarts it if not, and sends you a Telegram ping either way. It also repairs the trust flag above and warns you if your Claude login has genuinely expired.

These are **LaunchAgents**, so they only run while you're logged in and the Mac is awake. That's a requirement, not an oversight: the Claude CLI reads its login from your login keychain, which isn't available to a system daemon.

If a reel is interrupted mid-processing (restart, sleep, crash), it's recorded in `pending.json` and retried on the next start — up to three times, then it tells you it gave up.

## Configuration

Everything optional lives in [.env.example](.env.example) with comments. The ones worth knowing:

| Variable | Default | What it does |
|---|---|---|
| `APIFY_TOKEN` | *(unset)* | Paid. Adds a **spoken transcript** and is more reliable than yt-dlp. Without it everything still works — see below. |
| `NOTION_TOKEN` + `NOTION_DATABASE_ID` | *(unset)* | Sync notes to a Notion database. Skipped silently if unset. |
| `CLAUDE_MODEL` | *(your default)* | e.g. `claude-sonnet-4-6`. A stronger model gets links right more often. |
| `VIDEO_FRAMES` | `6` | Frames sampled per video for on-screen text. `0` disables. |
| `RICH_MESSAGE` | `1` | Telegram Rich Messages. Set `0` for plain HTML. |
| `SERVICE_LABEL` | `com.<user>.reel-to-action` | launchd service name. |

### Free vs. paid

**It runs with no paid services at all.** yt-dlp fetches public reels anonymously, ffmpeg samples the frames, and the agent reads them. What you lose without `APIFY_TOKEN` is the **spoken transcript** — notes are built from the caption and on-screen text, and the note says so plainly instead of pretending. For a lot of reels (lists, carousels, text-on-screen) that's barely a downgrade; for a talking-head interview it is.

yt-dlp's Instagram support **needs 2026.07.04 or newer** — older builds fail with "empty media response". `doctor.py` checks the version for you.

**Do not add Instagram cookies.** People get their accounts permanently banned for scraping with them, including on their own posts. Public reels don't need them.

### Claude login vs. API key

| | Subscription (`claude` → `/login`) | `ANTHROPIC_API_KEY` |
|---|---|---|
| Cost | Included in your plan | ~$0.05–0.10 per reel |
| Expiry | **Expires every few months** — the bot stops until you log in again | Never |
| Best for | Trying it out | Leaving it running |

The watchdog warns you when a login expires, but the API key is the set-and-forget option.

## Notion setup (optional)

Create a database, share it with an internal integration ([notion.so/my-integrations](https://www.notion.so/my-integrations)), and put the token and database id in `.env`.

You don't have to match a schema. `notion.py` reads your database's live columns and only fills the ones that exist, so you can rename and add freely. It looks for: `Source` (url), `Date` (date), `Author`, `Platform`, `Summary`, `Hook / key idea`, `Takeaways`, `Category` (multi-select), `Items`. **`Source` as a url column is the one worth adding** — deduplication and date preservation use it.

## Security notes

Reel captions, transcripts and web pages are **untrusted text written by strangers**, and they go into a prompt for an agent with tools. Two consequences:

- The agent's permission list is deliberately minimal — read the downloaded images, search the web, return JSON. **No shell access, no write access.** If you add tools there, a hostile caption can reach them.
- `ALLOWED_USER_IDS` fails closed.

The bot's own logs are gitignored, and `.env` is never committed. Telegram request URLs contain your bot token, so httpx logging is turned down to keep it out of the log file.

## Limitations

- **macOS only.** The service layer is launchd; the rest is portable but untested elsewhere.
- **Instagram and X are first-class.** Instagram uses Apify (spoken transcript) with yt-dlp as fallback; X/Twitter uses Apify's tweet scraper, which handles text, photo and video posts — yt-dlp only understands tweets containing video. YouTube and TikTok go through yt-dlp and mostly work. **Plain articles and newsletters don't yet** — anything with no video fails, since nothing fetches the page text.
- **Acquisition breaks periodically.** Instagram changes things; yt-dlp catches up within days. Keep it updated.
- **Single user.** One person, one machine, a JSON-file ledger. Sharing it with friends means one install each.
- Verified links are checked by an LLM with web search. ✅ means it found a canonical page — not a human guarantee.

## License

MIT — see [LICENSE](LICENSE).
