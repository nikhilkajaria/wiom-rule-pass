# BFC-VOLUME kill + prune pass -> Slack

Every morning after the ETL this reads the Growth Dashboard + Meta (both read-only), applies the
BFC-VOLUME kill + prune rules per the **Operating Spec (post SD sign-off, 23 Jun 2026)**, and posts
the day's decisions to **#growth-reports** (plus a DM copy) for one-click review in Ads Manager.

**It never writes to any ad platform.** Pausing / scaling stays a manual, human step.

## What it does
**Daily** (`--mode daily`, 07:00 IST): on the mature geo (Delhi), BOOKNOW-only, **lifetime** basis,
**active-only median** (Meta `effective_status` filter), first-spend anchored:
- **Efficiency kill** - CPBFC >= layer-multiplier x median (L1/L2 = 1.0x, L3 = 1.2x held), >=5 lifetime BFC.
- **Zero-BFC kill** - 0 BFC and >= Rs10,000 lifetime spend.
- **Cost-velocity brake** - spend >= max(5xC*, Rs15,000) and CPBFC >= 2x kill line -> KILL-REVIEW (human look).
- **Pool-cap prune** - if the active pool > 15, cut the weakest (protect layer x need-state coverage, then rank by delivery-velocity minus inefficiency). No per-layer floor.

**Weekly** (`--mode weekly`, Mon 07:00 IST): ISOLATE candidates (<=0.7x median, >=12 BFC) + geo budget (SCALE/HOLD vs C*) + geo conversion (CAP/CUT).

No calendar grace (the 5-BFC gate is the young-creative protection). If Meta is unavailable the run degrades to an all-eligible median and flags `active-filter OFF` rather than failing. All thresholds are constants at the top of `rule_pass.py`.

## Run locally (reads `C:\credentials\.env`)
```
python rule_pass.py --mode daily  --dry-run            # print, no post
python rule_pass.py --mode daily  --dm-only            # live test: posts to the DM only, skips the channel
python rule_pass.py --mode daily  --date 2026-06-22    # anchor D-1 to a specific day
```

## Setup
### Repo secrets (Settings -> Secrets and variables -> Actions)
- `WIOM_DASHBOARD_TOKEN`, `META_ACCESS_TOKEN`, `SLACK_BOT_TOKEN` (all set).

### Slack
- Bot scopes: `chat:write`, `im:write`.
- **Invite the bot to #growth-reports** (`/invite @<bot>`), else channel posts fail with `not_in_channel`. The DM copy needs no invite.
- Override target via env if needed: `SLACK_CHANNEL_ID` (default `C0B9G0Q68G6` = #growth-reports), `SLACK_DM_USER_ID` (default `U05A9037VFG`).

### Schedule
`.github/workflows/*.yml` cron is **UTC**. `30 1 * * *` = 07:00 IST daily; `30 1 * * 1` = Mon 07:00 IST weekly. Keep ~30-60 min after the ETL.

## Notes
- Read-only: emits decisions, never pauses.
- A "no kills, no brake, no prune" message is posted on clean days, so a silent day means the job did not run.
- `META_ACCESS_TOKEN` should be a long-lived / system-user token; if it expires the active-filter degrades (flagged) until rotated.
