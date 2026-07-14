# BFC-VOLUME kill + prune pass -> Slack

Every morning after the ETL this reads the Growth Dashboard + Meta (both read-only), applies the
BFC-VOLUME kill + prune rules per the **Operating Spec v2.2.0 (post SD sign-off, 1 Jul 2026)**, and posts
the day's decisions to **#growth-reports** (plus a DM copy) for one-click review in Ads Manager.

**It never writes to any ad platform.** Pausing / scaling stays a manual, human step.

## What it does
**Daily** (`--mode daily`, 07:00 IST): on the mature geo (Delhi), BOOKNOW-only, **lifetime** basis,
**active-only median** (Meta `effective_status` filter), first-spend anchored:
- **Efficiency kill** - CPBC >= layer-multiplier x median (L1/L2 = 1.0x, L3 = 1.2x held), >=5 lifetime BC.
- **Zero-BC kill** - 0 BC and >= Rs10,000 lifetime spend.
- **Cost-velocity brake** - spend >= max(5xC*, Rs15,000) and CPBC >= 2x kill line -> KILL-REVIEW (human look).
- **Daily kill cap** - if efficiency-kill candidates > 3, rank by CPBC/median ratio (worst first) and cap at 3 per day. Rest roll to MONITOR.
- **Top-spender warning** - if a KILL candidate is #1 or #2 by 7-day daily avg spend AND holds >10% of pool avg spend, the Slack verdict gets a "TOP SPENDER - scale replacement before pausing" label. No gate, operator decides.
- **Pool-cap prune** - if the active pool > 15, cut the weakest (protect layer x need-state coverage, then rank by delivery-velocity minus inefficiency). No per-layer floor.

**Weekly** (`--mode weekly`, Mon 07:00 IST): ISOLATE candidates (<=0.7x median, >=12 BC) + geo budget (SCALE/HOLD vs C*) + geo conversion (CAP/CUT).

No calendar grace within the first 7 days (the 5-BC gate is the young-creative protection); past that, AGE_GRACE_DAYS makes a thin, still-bad performer kill-eligible even below the gate. If Meta is unavailable the run degrades to an all-eligible median and flags `active-filter OFF` rather than failing. All thresholds are constants at the top of `rule_pass.py`.

**Naming note:** this codebase historically labeled the booking metric "BFC" (CPBFC, 5-BFC gate, etc.) even though it has always computed against the `booking_confirmed` API field, never `booking_fee_captured` - the latter undercounts by ~3x since a booking-fee waiver took effect ~24 Jun 2026. Renamed to "BC"/"CPBC" throughout as of 2026-07-13 for clarity. **`BFC-VOLUME` is unrelated** - it's the literal Meta/Google campaign-family name and is never renamed.

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
