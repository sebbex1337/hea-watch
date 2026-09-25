# hea-watch

Notifies you on Discord when HEA's garage at **Uranienborg Allé 1, Søborg** stops
being `udlejet` — and when any new garage shows up in their P-plads category.

Runs on GitHub Actions. Free, no server.

[The listing](https://hea.dk/lejemaal/p-plads/garage-udlejes-pa-uranienborg-alle-i-soborg/)

## Setup

1. Create a **public** GitHub repo and push `check.py`, `test_check.py`,
   `.github/workflows/watch.yml`, `.gitignore` and this README.
2. Discord: **Server Settings → Integrations → Webhooks → New Webhook**, pick a
   channel, **Copy Webhook URL**.
3. GitHub: **Settings → Secrets and variables → Actions → New repository secret**,
   name it `DISCORD_WEBHOOK`, paste the URL.
4. **Actions** tab → "Watch HEA garage" → **Run workflow**, tick **"Send test Discord messages"**, and confirm two messages land in the channel. Then run it again unticked to take the baseline.

The first run records the baseline and stays quiet. Every run after that alerts on
any change. Install the Discord phone app or you won't feel the buzz.

## The part that makes this non-trivial: the WAF

hea.dk is behind Simply.com's firewall. A plain `requests.get()` gets **HTTP 454
"Checking your browser"** — the site hands every client a JavaScript proof-of-work
puzzle and only serves the page once it's solved.

So this drives a real headless Chromium via Playwright, which solves the puzzle the
way your own browser does. That costs ~60s per run instead of ~2s, which is why the
repo should be public (unlimited Actions minutes).

`load_page()` waits for the challenge to clear, then re-requests so the HTTP status
it records is the real one rather than the challenge's 454.

## Why it doesn't guess what "available" looks like

Nobody knows what HEA writes when the garage frees up — it might say `ledig`, drop
the badge, get a new URL, or vanish. So the script never tests for an "available"
word. It fingerprints the listing's own text block (top of page down to
"Andre lejemål") and alerts on **any** difference, showing a diff.

`TAKEN_WORDS` is used *only* to set urgency, never to decide whether to alert:

| Page says | Alert |
|---|---|
| `udlejet` / `optaget` / `reserveret` still present | quiet — text changed, probably a price or wording edit |
| any of those gone, or replaced by anything at all | **loud `@everyone`** |
| page 404s | **loud** — rented and pulled, or moved |
| identical text but `og:updated_time` moved | quiet — HEA touched the page |
| 3 failed runs in a row | **loud** — the watcher itself is broken |

That last row matters: a scraper that dies silently is worse than no scraper.

Two subtleties the tests pin down:

- The page's own heading says "Garage **udlejes** på…" — a word meaning *is for
  rent* — while the badge says **udlejet**, *rented out*. A first-match status scan
  latches onto the heading and concludes the garage is free forever, so it never
  registers the flip. `find_statuses()` collects *every* status word and
  `is_taken()` returns true if any of them means taken.
- Absence of evidence counts as good news. An unrecognised status word, or no badge
  at all, is treated as possibly available — never as a reason to stay quiet.

Dates and cookie banners are filtered out so they don't cause daily noise. Edits to
the *other* listings in "Andre lejemål" are excluded from the fingerprint.

`state.json` accumulates `seen_status_phrases` — every distinct status word the page
has shown. After a few weeks you'll know HEA's actual vocabulary and can tighten
`TAKEN_WORDS` from evidence.

Expect some false positives early on. That's the trade for not missing the real one.

## Tests

```bash
python test_check.py
```

No browser or network needed — they feed fixture text through the parsing and assert
the alert decisions, including the `udlejes`/`udlejet` trap above.

## Checking the Discord wiring

A normal run is silent when nothing has changed, so a green tick proves the scraper
works and tells you nothing about Discord. To test the notification path itself:

**Actions → Watch HEA garage → Run workflow → tick "Send test Discord messages".**

That sends two messages through the same `notify()` the real alerts use — one quiet,
one `@everyone`. Both should arrive.

- **Neither arrives:** the `DISCORD_WEBHOOK` secret is wrong or misnamed. The run log
  prints the HTTP status Discord returned (401/404 = bad or deleted webhook).
- **Only the quiet one arrives:** the webhook works but `@everyone` is blocked by that
  channel's permissions — fix it in **Channel Settings → Permissions**, or the alert
  you actually care about will land without a ping.

Locally: `DISCORD_WEBHOOK='https://...' python check.py --test`

## Schedule: why the clock lives outside GitHub

**GitHub's cron does not work for this.** Measured over three days on this repo:

| | Scheduled | Actually ran | Median gap |
|---|---|---|---|
| every 20 min | 42/day | 5-6/day | 4h (worst 7.1h) |
| every 30 min | ~20/day | 3/day | 11.8h |

Lowering the frequency made it *worse*. Scheduled workflows are best-effort and
GitHub simply drops most of them; delivered runs arrived 2-3 hours after their slot.
No cron expression fixes this.

Runs triggered **on demand**, though, start within a second — the executor was never
the problem, only the clock. So an external cron service POSTs a `repository_dispatch`
and GitHub runs the check immediately:

```
cron-job.org  --every 30 min-->  POST /repos/<you>/hea-watch/dispatches
                                            |
                                            v
                                GitHub Actions runs check.py --> Discord
```

The `schedule:` block is kept at three slots a day purely as a fallback if the
external trigger dies. Don't rely on its timing.

### Setting up the trigger

1. **GitHub → Settings → Developer settings → Personal access tokens → Fine-grained
   tokens → Generate new token.**
   - Repository access: **Only select repositories** → `hea-watch`
   - Repository permissions: **Contents → Read and write** (this is what `dispatches`
     requires; nothing else needs enabling)
   - Set an expiry you'll actually notice, and diary a reminder to rotate it.
2. **[cron-job.org](https://cron-job.org) → Create cronjob:**
   - URL: `https://api.github.com/repos/<you>/hea-watch/dispatches`
   - Schedule: every 30 minutes (it does handle `Europe/Copenhagen` properly, unlike
     Actions, so you can restrict it to 08:00-16:00 weekdays here rather than in cron)
   - Method: **POST**
   - Headers: `Accept: application/vnd.github+json`, `Authorization: Bearer <token>`
   - Body: `{"event_type":"check-now"}`
3. Save, hit **Test run**, and confirm a `repository_dispatch` run appears in the
   Actions tab. A correct call returns **204 No Content** with an empty body.

That token can write to this repo, so scope it to this repo alone. If you'd rather it
didn't sit in someone else's service, a Cloudflare Worker cron trigger does the same
job with the token in your own account.

### Making dropped runs visible

`check_staleness()` alerts (quietly) when more than 4 hours pass with no successful
check — which is how you find out the external trigger has stopped, rather than
mistaking silence for "nothing has changed".


## Notes

- `state.json` is committed back after each run — that's the memory. The workflow
  rebases before pushing so overlapping runs can't conflict.
- Each run uploads a `page-snapshot` artifact (text + screenshot) kept 7 days. If an
  alert looks wrong, that shows you exactly what the browser saw.
- If runs start failing on HTTP 403/454, lower the frequency first.
- Turn it off: disable the workflow in the Actions tab, or delete the repo.

## Don't rely on this alone

A scraper is the backup, not the plan:

- Fill in **"Opret søgeagent"** on hea.dk — their own alerts, no scraping involved.
- **Call +45 33 31 45 00**, weekdays 9:00–12:00, and ask to go on the waiting list
  for Uranienborg Allé 1. For a single garage, a human remembering your name beats
  any polling loop.
