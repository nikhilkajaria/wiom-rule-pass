# BFC-VOLUME Creative Kill/Prune Pass - Session Handoff

Objective of this doc: let a fresh session run the BFC-VOLUME creative-optimization
"kill pass" standalone - the Slack automation repo, the current ratified framework, the
full change history WITH reasoning (docs + Slack), and the open calls still pending SD
(Satyam / @SD) alignment. Read top to bottom before acting.

Owner: Nikhil (runs it). Framework owner/ratifier: SD (Satyam Darmora). Learnings-eval
ownership moved to Shiva on 9 Jun. Channel: #growth-function (C0B3445V5AP) for discussion;
the pass posts to #growth-reports (C0B9G0Q68G6).

NOTE: this is NOT the G1/G2 creative gate ("gate pass"). This is the performance
kill/scale optimizer for the BFC-VOLUME scale campaign. (The gate-pass handoff is separate:
`wiom-maker-checker/GATE_PASS_HANDOFF.md`.)

---

## 0. What it is (one paragraph)
A read-only morning job that reads the Growth Dashboard + Meta, applies the ratified
BFC-VOLUME kill + prune rules, and posts the day's KILL / BRAKE / PRUNE / ISOLATE decisions
to #growth-reports for one-click human execution in Ads Manager. It NEVER writes to any ad
platform - pause/scale stays a manual human step. "Kill fast, scale slow."

---

## 1. The repo
- Local: `C:\Users\nikhi\wiom-rule-pass`  |  single file `rule_pass.py` + GitHub Actions cron.
- Reads `C:\credentials\.env` (WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN, SLACK_BOT_TOKEN).
- Run:
  - `python rule_pass.py --mode daily --dry-run`     (print, no post)
  - `python rule_pass.py --mode daily --dm-only`      (live test -> DM only)
  - `python rule_pass.py --mode daily --date 2026-06-22` (anchor D-1 to a day)
  - `--mode weekly` for the weekly pass.
- Schedule (`.github/workflows/*.yml`, cron is UTC): `30 1 * * *` = 07:00 IST daily;
  `30 1 * * 1` = Mon 07:00 IST weekly. Keep 30-60 min AFTER the ETL.
- Slack: bot needs `chat:write`, `im:write`, and must be invited to #growth-reports
  (else channel post fails `not_in_channel`; DM copy needs no invite). Defaults:
  SLACK_CHANNEL_ID=`C0B9G0Q68G6`, SLACK_DM_USER_ID=`U05A9037VFG`.
- Safety: read-only; posts a "no kills/brake/prune" message on clean days (so silence =
  job didn't run). All thresholds are constants at the top of `rule_pass.py`.
- Git history (4 commits): `f18907a` daily+weekly v3 cadence -> `b803ebe` Week-1 grace ->
  `6235216` rewrite to kill+prune v2.1.2 (post SD sign-off) -> `c144f09` integrity line
  (states whether active-status was Meta-vetted vs dashboard-spend-only).

---

## 2. The framework AS RATIFIED (v2.1.2, post SD sign-off 23 Jun 2026)

Basis for the daily pass: mature geo = Delhi, BOOKNOW-only, LIFETIME basis, active-only
median (Meta `effective_status` filter), first-spend anchored, no calendar grace (the
5-BFC gate is the young-creative protection). BFC metric = booking_confirmed (canonical,
matches dashboard). NOTE: booking_fee_captured was used until 28 Jun and undercounted
badly from ~23 Jun onwards - new app flow variants introduced around that date do not
collect a booking fee, so fee-capture = 0 even when bookings happen. Do not use
booking_fee_captured for any creative launched post 23 Jun.

DAILY (07:00 IST):
- Efficiency kill - CPBFC >= layer-multiplier x active median (L1/L2 = 1.0x, L3 = 1.2x
  held), AND >= 5 lifetime BFC.
- Zero-BFC kill - 0 BFC and >= Rs 10,000 lifetime spend.
- Cost-velocity brake - spend >= max(5 x target-CPBL, Rs 15,000) AND CPBFC >= 2x the kill
  line -> KILL-REVIEW (human look), regardless of BFC count. (This is the "Rs 40K hole"
  catch - stops a creative that collected 1-4 bookings at a terrible CPBL from running
  uncapped because it isn't yet efficiency-kill-eligible.)
- Pool-cap prune - if active pool > 15: cut the weakest. Order: (1) drop anything already
  KILL/REPLACE, (2) keep CONTINUE + ISOLATE candidates, (3) from the rest protect
  coverage on AUDIENCE LAYER x NEED-STATE (NOT format), then rank by delivery-velocity
  minus inefficiency, cut to 15. NO per-layer survivor floor.

WEEKLY (Mon 07:00 IST):
- ISOLATE candidates - <= 0.7x median CPBL AND >= 12 BFC (recharge passes) -> own ad set /
  materially increase delivery. (In single-ad-set ABO, isolate is the ONLY creative-level
  scale lever.)
- Geo budget - SCALE/HOLD vs target C*.
- Geo conversion - CAP/CUT.

Degraded mode: if Meta is unavailable the run falls back to an all-eligible median and flags
`active-filter OFF` rather than failing.

---

## 3. CHANGE HISTORY (chronological, with reasoning - docs + Slack)
All Slack links are #growth-function (C0B3445V5AP).

CH-1 (9 Jun) - Nikhil's v1 performance-eval framework doc. Same day, the team killed the
separate CREATIVE-TESTING campaign idea (deploy in main campaign instead) and learnings-eval
ownership moved to Shiva.
  Doc: https://docs.google.com/document/d/1zH2hTFv4hZfjGfJsvEfjezfsJJ3bVYd7Z_CUQa9VAQ4
  Slack: .../p1781031355925309

CH-2 (18 Jun) - SD's 13-question audit: "I'm not sure the framework is being applied."
Asks which verdicts weren't executed, spend-after-kill-verdict, which winners were isolated,
verdict-to-execution lag, what could be a standing rule/automated alert, live circuit-breakers,
etc. This is the pressure that drove automation.
  Slack: .../p1781746133119839

CH-3 (22 Jun) - SD posts "Five operating rules go live today" + a v2.1 amendment doc:
(1) Pool cap 12-15/ad set, (2) Daily kill pass, (3) Cost-velocity brake, (4) Throttle state
(80% spend cut for high-spend MONITOR), (5) Isolate winners faster. PLUS: L3's 1.2x kill-buffer
to be removed in v2.1 but DO NOT pre-empt - it ships together with a protected 20% Exploration
("Discovery") budget, else the pool optimizes to L1 harvest and starves L2/L3 learning.
  Slack: .../p1782134667749679

CH-4 (22 Jun) - Nikhil's pushback on SD's doc (the two real corrections):
(a) THROTTLE is not executable in an ABO ad set - you can't cut one creative's spend; isolating
it at ~20% never exits learning. Proposed collapsing THROTTLE into MONITOR/KILL.
(b) DISCOVERY ad set design is fuzzy - is it separate from CREATIVE-TESTING (install-optimized,
Rs 500/day/creative)? How does an install-proven winner earn into a booking-optimized BFC-VOL?
(the graduation seam problem.)
  Slack: .../p1782143456001889

CH-5 (22 Jun) - Nikhil sets up the daily kill+prune pass off SD's v2.1.2 rules and asks for
three prune baselines (cap 12 vs 15; diversity cell definition; minimum survivors per layer).
Attaches a "current state of kill framework + open calls" doc.
  Slack: .../p1782153957846219

CH-6 (22 Jun) - Nikhil's day-3 CPI analysis: day-3 CPI is the one robust EARLY loser-signal
(catches ~85% of eventual losers); value is SPEED not savings; best as a REVIEW FLAG, not an
auto-kill. Could feed two open calls: graduation screen (CT -> BFC-VOL) and prune-ranking for
un-matured creatives. Caveat: the CPI->CPBFC link is measured inside BFC-VOLUME, not yet on CT.
  Slack: .../p1782158899210939

CH-7 (23 Jun) - SD's SIGN-OFF (the ratification the repo encodes):
- PRUNE BASELINES: cap = 15 (headroom; tighten to 12 later if needed); diversity cell =
  AUDIENCE LAYER x NEED-STATE, NOT format (format floats free); minimum survivors per layer =
  NONE (a survivor floor would re-leak the L3 subsidy; category protection is the Discovery
  box's job, not the prune's; BFC-VOLUME ending mostly-L1 is the correct outcome).
- THROTTLE: dropped (Nikhil's ABO point accepted); MONITOR-until-prune/kill replaces it.
- L3 BUFFER FLIP: holds at 1.2x until ALL THREE are true - Discovery funded at 20% floor,
  Discovery reporting active, Discovery roster populated. Then flips to flat 1.0x
  automatically (condition-based, no separate sign-off).
- GRADUATION SEAM (CT -> BFC-VOLUME): stays OPEN/manual. SD is working a bridge metric; do
  NOT hard-code any graduation rule until closed.
  Slack: .../p1782177444820339

CH-8 (24-25 Jun) - first live consequences + the cascade. Applying a kill (T-050) collapsed
Meta spend; as creatives were culled Meta concentrated spend onto individual creatives
(distributed -> concentrated). Diagnosis: cascade concentration after aggressive kills removed
the alternatives; T-050 went ~3% -> 46% -> 64% of pool spend over 2 days; the Jun 23 kill was
CORRECT on its data (CPBL ~Rs 18,805) but the pool was too thin to absorb the hole.
  Slack: .../p1782306815042049

CH-9 (27-28 Jun) - logging + retrospective action-check added to the pass. Each live run now
appends KILL recos to kill_pass_log.json (committed to repo) and the Google Sheet
(1ay8EBJ3... -> "BFC-VOLUME Kill Pass Log", tab "Recos"). Next morning's run back-fills
action_taken (Yes/No) and action_timing (On-time/Late) via Meta configured_status +
updated_time. Unacted KILLs surface in the next day's Slack post.
  Commit: c144f09 (logging) -> 4a104c7 (metric fix, see CH-10)

CH-10 (28 Jun) - METRIC BUG FIXED: pass was using booking_fee_captured instead of
booking_confirmed for CPBFC. Root cause: app flow variants introduced ~23 Jun do not
collect a booking fee, so fee-capture = 0 even when bookings confirm - all post-23 Jun
creatives were effectively invisible to the pass. Discovered via C-069: 5 fee-captured
vs 41 booking_confirmed -> CPBFC Rs 18,753 vs Rs 2,232. The morning pass on 28 Jun
incorrectly fired the cost-velocity brake on C-069 and an efficiency kill on C-039
(and may have caused unnecessary pauses). Fix: all CPBFC calculations now use
booking_confirmed, matching the dashboard canonical metric.
  Commit: 4a104c7 on feat/kill-prune-v2.1.2

---

## 4. OPEN CALLS - yet to be aligned / ratified with SD

1. GRADUATION SEAM (CT -> BFC-VOLUME). CT is install-optimized, BFC-VOLUME is booking-
   optimized, and a single-creative booking ad set can't exit learning - so an install-proven
   winner isn't proven on what BFC-VOLUME cares about. SD working a bridge metric; seam is
   MANUAL until closed. Nikhil's day-3-CPI is a candidate graduation screen (review flag only).

2. DISCOVERY / EXPLORATION BOX (the 20% protected budget). Not yet stood up. The L3 buffer
   flip is gated on it (3 conditions). Until it exists, L3 keeps 1.2x. Design still fuzzy
   (separate from CT? optimize on install or PBFC? how does a winner graduate?).

3. CONCENTRATION ALARM (proposed by Nikhil, NOT yet raised/ratified). Out of the T-050
   cascade: if any single creative exceeds ~35-40% of ABO daily spend, flag structural risk;
   before executing a kill of a dominant creative, require >=2 viable alternatives at >5%
   spend share each (add alternatives first, then kill, or kill at low-traffic hours). The
   current framework has a cost-velocity brake but NO concentration check - this is the gap
   the cascade exposed. RAISE WITH SD.

4. DAY-3 CPI as a standing review flag (CH-6). Partially accepted as a flag, not an auto-kill;
   CPI->CPBFC link only measured inside BFC-VOLUME (not CT) - confirm before wiring in.

5. SD's 13-question audit (CH-2) - several are standing-rule / automation candidates not yet
   all closed (verdict-to-execution lag tracking, "which decisions are you now making before
   I ask", live circuit-breakers without a human).

6. Google UAC (adjacent, separate from Meta kill-pass): tCPA removed ~18 Jun -> installs
   down ~38% but CPBL improved ~40% (algo cut wasteful Display). Reco flagged to SD: restore
   tCPA ~Rs 1,800-2,000 to re-anchor before the unproven YouTube ramp absorbs budget. Not yet
   actioned. (The rule-pass is Meta/BFC-VOLUME only; UAC is managed separately.)

---

## 5. Source materials
- Nikhil v1 framework doc: `1zH2hTFv4hZfjGfJsvEfjezfsJJ3bVYd7Z_CUQa9VAQ4`.
- SD's v2.1 amendment doc + Nikhil's v2.1.2 "current state + open calls" doc: Slack
  attachments in the CH-3 / CH-5 threads (pull the files from those permalinks if needed).
- The repo README (`wiom-rule-pass/README.md`) is the canonical spec of the running rules.
- Slack channel: #growth-function (C0B3445V5AP). Posts land in #growth-reports (C0B9G0Q68G6).

## 6. Data + tools
- Growth Dashboard API (base https://growth-portal.up.railway.app, header X-Dashboard-Token
  from `C:\credentials\.env`). Key endpoints: `/api/decision_layer?camp_set=bfc_volume`
  (budget vs targets + creative perf incl. is_active/status_label/cpbl/bfc/view_rate),
  `/api/war_room`, `/api/raw/day|days`, `/api/master_export`. Full dictionary in ~/.claude/CLAUDE.md.
- Meta Marketing API: act `2007675312900454`, v23.0, token in `C:\credentials\.env` (or
  meta-ads-dashboard/.env). Used READ-ONLY for active-status + engagement; per ads-api-safety
  ANY write (pause/budget) needs explicit human approval - the pass deliberately never writes.
- ABO reality: budget is set at ad-set level and Meta distributes across creatives - there is
  NO creative-level spend control, which is WHY throttle was dropped and isolate is the only
  scale lever.

## 7. How to continue (next session, kill-pass only)
1. `cd C:\Users\nikhi\wiom-rule-pass` ; `python rule_pass.py --mode daily --dry-run` to see
   today's decisions without posting.
2. To change a rule: edit the constants at the top of `rule_pass.py`; keep README + the
   framework in sync; material rule changes need SD sign-off (see CH-7 pattern).
3. Priorities to push on with SD: the CONCENTRATION ALARM (#3 above - the cascade gap), the
   GRADUATION SEAM (#1), and standing up the DISCOVERY box (#2, which unlocks the L3 flip).
4. Credentials from `C:\credentials\.env`; never print secrets; read-only by default.
