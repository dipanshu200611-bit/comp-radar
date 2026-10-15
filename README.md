# Competition Radar

One bot that discovers quant/finance, consulting and product-management competitions worldwide,
stores them in `data/`, serves a live dashboard, and sends alerts. No hardcoded site list.

## Quick start
    pip install -r requirements.txt
    python comp_radar.py --serve --daily 08:00
Open http://127.0.0.1:8000. First run scans automatically (takes several minutes). Use "Scan now" any time.

## Commands
    python comp_radar.py                        one scan, all categories
    python comp_radar.py --profile product      finance | consulting | product | all
    python comp_radar.py --online-only          drop in-person events (finance is online-only by default)
    python comp_radar.py --include-offline      keep in-person events everywhere
    python comp_radar.py --serve --host 0.0.0.0 reachable from other devices on your network

## Free daily hosting (GitHub)
1. Push this folder to a GitHub repo.
2. Settings > Secrets > Actions: add ANTHROPIC_API_KEY (and TELEGRAM_*/DISCORD_WEBHOOK if wanted).
3. Settings > Pages: deploy from the main branch root. Your dashboard is at https://<you>.github.io/<repo>/
The workflow in .github/workflows/daily.yml scans at 08:00 IST and commits data/ back.

## Config (.env, see .env.example)
ANTHROPIC_API_KEY (accurate page reading), TELEGRAM_TOKEN + TELEGRAM_CHAT_ID, DISCORD_WEBHOOK,
EXTRA_QUERIES, MAX_PAGES, WORKERS, SEARCH_DELAY, REMIND_DAYS.

## Outputs
data/competitions.json (database), .csv, .md, meta.json (last scan).
Alerts: every new find, plus a reminder when a deadline is within REMIND_DAYS (default 3).
