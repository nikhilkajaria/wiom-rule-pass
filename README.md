# Daily BFC-VOLUME rule-pass -> Slack

Every morning after the ETL, this reads the Growth Dashboard (read-only), applies the
BFC-VOLUME kill rules on BOOKNOW-only / rolling-7-day / D-1 data, and DMs you the flagged
kills so you can one-click review and pause in Ads Manager.

**It never writes to any ad platform.** Pausing stays a manual, human step.

## What it flags (v1)
- **ZERO-BFC BLEED** - a concept with >= Rs10,000 of 7-day BOOKNOW spend and 0 bookings (circuit-breaker).
- **EFFICIENCY-KILL** - a concept whose 7-day BOOKNOW CPBL is >= its ad-set median (L1/L2) or >= 1.2x median (L3), with a Rs3,000 min-spend gate and >= 3 peers for a trustworthy median.

Thresholds live at the top of `rule_pass.py` (`ZERO_BFC_SPEND`, `MIN_SPEND_7D`, `MIN_PEERS`, `KILL_MULT`). Confirm them against the framework doc, then tune in that one place.

## One-time setup

### 1. Create the Slack app + bot token
1. https://api.slack.com/apps -> Create New App -> From scratch -> workspace `wiomworkspace`.
2. OAuth & Permissions -> Bot Token Scopes -> add `chat:write` and `im:write`.
3. Install to Workspace -> copy the **Bot User OAuth Token** (`xoxb-...`).

### 2. Create a PRIVATE GitHub repo and push these files
```
cd C:\Users\nikhi\scripts\rule-pass-digest
git init && git add . && git commit -m "Daily BFC-VOLUME rule-pass"
# create a PRIVATE repo on github.com, then:
git remote add origin <your-private-repo-url>
git push -u origin main
```

### 3. Add repo secrets (Settings -> Secrets and variables -> Actions -> New repository secret)
- `WIOM_DASHBOARD_TOKEN` = value of WIOM_DASHBOARD_TOKEN from `C:\credentials\.env`
- `SLACK_BOT_TOKEN` = the `xoxb-...` token from step 1

(No secrets ever go in the code. The repo must be private regardless.)

### 4. Set the schedule
Edit `.github/workflows/daily-rule-pass.yml` -> the `cron` line. It is **UTC**.
`30 4 * * *` = 10:00 IST. Set it ~30-60 min after your ETL finishes.

## Test before trusting it
Locally (reads `C:\credentials\.env`, prints, does not post):
```
python rule_pass.py --dry-run
python rule_pass.py --dry-run --date 2026-06-17     # anchor D-1 to a specific day
```
In GitHub: Actions tab -> "Daily BFC-VOLUME rule-pass" -> Run workflow (once the bot token secret is set, this posts to your DM).

## Notes
- GitHub Actions cron runs in UTC and can be delayed a few minutes under load.
- A "no kill-flags today" message is sent on clean days, so a silent day means the job did not run.
- v1 has no geo-ceiling (C*) check and is not serviceability-adjusted - that is a planned v2.
