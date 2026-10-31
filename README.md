# Maintainerr Telegram Companion

A self-hosted Python companion that posts Telegram-native polls for media in [Maintainerr](https://github.com/jorenn92/Maintainerr) collections, records voting outcomes, and manages rule-specific Maintainerr exclusions.

**Maintainerr remains responsible for deleting media.** This project never calls a media-delete endpoint. It **does** modify Maintainerr state: it resets a title's retention timer when creating a poll, and it adds/removes rule exclusions in response to votes. If you enable deletion in Maintainerr, that independent deletion schedule still applies.

## How it works

For each new title in your configured Maintainerr collections, the bot sends a small TMDb poster followed by a Telegram-native poll replying to that photo.

| Vote | Result after poll closes |
| --- | --- |
| 🛡️ Prevent deletion (veto) | **Any single veto wins**, even if others voted differently. Create a permanent exclusion for the corresponding Maintainerr rule group. |
| ⏳ Postpone for 60 days | If there are no vetoes, create a temporary Maintainerr rule exclusion, record its ID, and remove *only that bot-owned ID* after 60 days. |
| 🗑️ Deletion is okay | If neither of the above received a vote, leave the title unexcluded; Maintainerr's own configured action decides what happens. |

Polls normally close after **29 days** (configurable). The 60-day postponement starts when the poll **actually closes**. A manually closed poll uses its earlier closing date.

**Limitations to understand before enabling deletion:**
- Maintainerr's media deletion timer runs independently of Telegram polling. A prolonged outage can prevent votes being processed before Maintainerr deletes media. **There is no fail-safe startup ordering or distributed transaction between these services.**
- At poll creation, this bot calls Maintainerr's postpone endpoint to reset the timer to the configured retention interval. Ensure Maintainerr is configured with the same interval.
- After a 60-day exclusion expires, the bot removes it and waits for Maintainerr to surface the title in the collection again. It does **not** force a rule re-evaluation; a new poll is conditional on the title reappearing.
- Manually added Maintainerr collection items may behave differently from rule-matched items after an exclusion expires.
- The bot keeps SQLite state locally; don't delete this database while polls or exclusions are active.
- A cover and its poll are **two Telegram messages**, not a single image-embedded poll.

## Requirements

- Docker with Compose v2, or Python 3.13+
- A working Maintainerr deployment whose API is reachable by the companion
- A Telegram bot token (created through BotFather)
- The target Telegram chat ID and permission for the bot to post and close polls
- The existing Docker network that connects Maintainerr to its companion

This companion uses only the Python standard library; no pip dependencies.

## Setup

1. Clone this repo and run `cp .env.example .env`.
2. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in `.env`. Never commit `.env`.
3. Set `MAINTAINERR_URL` and `COLLECTION_IDS` for your installation. The example values are illustrative, not universal.
4. Set `MAINTAINERR_NETWORK` in your shell or in the shell's Compose environment if your existing external Docker network is not named `compose_default`.
5. Leave `PUBLISH_POLLS=false` while validating API access, exclusions, permissions, retention settings, and expected media.
6. Run `docker compose up -d`, then inspect `docker compose logs -f maintainerr-telegram-polls`.
7. Only once ready, change `PUBLISH_POLLS=true` in `.env` and recreate the container with `docker compose up -d --force-recreate`.

**A new container starts a reconciliation cycle immediately.** With publication enabled it may publish up to `MAX_NEW_POLLS_PER_CYCLE` polls upon startup; subsequent cycles use `POLL_INTERVAL_SECONDS`. Do not repeatedly restart the service unless you intend to accelerate publication.

### Configuration

| Variable | Example default | Purpose |
| --- | --- | --- |
| `MAINTAINERR_URL` | `http://maintainerr:6246` | Maintainerr API on the shared Docker network |
| `COLLECTION_IDS` | `1,2` | Comma-separated Maintainerr collection IDs |
| `TELEGRAM_BOT_TOKEN` | *required* | Private Telegram bot token |
| `TELEGRAM_CHAT_ID` | *required* | Target chat or supergroup ID |
| `PUBLISH_POLLS` | `false` | Enable new poll publication |
| `POLL_INTERVAL_SECONDS` | `2400` | Seconds between reconciliation cycles |
| `MAX_NEW_POLLS_PER_CYCLE` | `2` | Upper bound on new polls per cycle |
| `POLL_DAYS` | `29` | Normal poll closing age in days |
| `RETENTION_DAYS` | `45` | Expected Maintainerr retention in days |
| `MAINTAINERR_NETWORK` | `compose_default` | External Docker network name (Compose substitution) |

## Closing polls early

The separate CLI shares the running bot's file lock so it cannot process polls concurrently. **Preview is the default; only `--execute` closes polls and applies vote results.**

```bash
# Preview every open poll that is at least 3 days old
docker compose run --rm -T maintainerr-telegram-polls python close_polls.py --days 3

# Actually close and evaluate those polls
docker compose run --rm -T maintainerr-telegram-polls python close_polls.py --days 3 --execute

# Preview *all* currently open polls, regardless of age
docker compose run --rm -T maintainerr-telegram-polls python close_polls.py --days 0
```

The age range is 0 to `POLL_DAYS`; fractional days are accepted. `--days 0 --execute` is intentionally destructive to **poll state**, even though it does not delete media. It may cause permanent exclusions and should be used carefully.

## State and operations

- Poll state, round history, exclusion ownership, and Telegram message IDs are stored in `polls.sqlite` in the mounted project directory.
- Back up the database together with its SQLite WAL state using SQLite's backup API while the service is running.
- Polls cannot be reliably reconstructed from Telegram after deleting the database.
- Maintainerr rule exclusions are separate from Radarr/Sonarr/Jellyfin tags.
- The bot has no admin web UI and no incoming Telegram commands; management uses the CLI.

## Development

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q bot.py voting.py covers.py close_polls.py
```

## Security and publishing

The repository includes `.env.example` only. Never publish a real Telegram token, chat ID, database, message history, backup, or diagnostic logs. The Git ignore rules prevent accidental addition of the common local files, but always review `git diff --cached` before pushing.

**License:** MIT — see [LICENSE](LICENSE). This is an independent community companion, not an official Maintainerr project.
