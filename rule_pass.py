# -*- coding: utf-8 -*-
"""BFC-VOLUME kill + prune pass -> Slack. Operating Spec v2.2.0 (post SD sign-off, 1 Jul 2026).

  DAILY (post-ETL): efficiency kill + zero-BC kill + cost-velocity brake (KILL-REVIEW) + pool-cap
        prune cut-list. Lifetime CPBC (booking_confirmed), lifetime 5-BC gate, NO calendar grace,
        active-only median (Meta effective_status filter), first-spend anchoring. Delhi only.
  WEEKLY (review): isolate candidates + geo budget (SCALE/HOLD vs C*) + geo conversion (CAP/CUT).

  LOGGING: each run appends KILL recos to kill_pass_log.json and the Google Sheet (SHEET_ID).
           Next day's run back-fills action_taken (Yes/No) + action_timing (On-time/Late) via
           Meta API read of configured_status + updated_time. Unacted KILLs surface in the post.

It NEVER writes to any ad platform - pausing/scaling stays a manual human step. Posts to
#growth-reports and a DM copy.

Run:  python rule_pass.py --mode daily  [--dry-run] [--dm-only] [--date YYYY-MM-DD]
Env (Actions secrets / local C:\\credentials\\.env): WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN,
     SLACK_BOT_TOKEN; optional SLACK_CHANNEL_ID, SLACK_DM_USER_ID, META_AD_ACCOUNT_ID,
     META_API_VERSION, GOOGLE_SERVICE_ACCOUNT_JSON.
"""
import sys, io, os, json, re, argparse, datetime, statistics, urllib.request, urllib.parse
import collections, subprocess
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- spec constants (v2.2.0, SD sign-off 1 Jul 2026) ----
CAMPAIGN_START    = '2026-06-01'
WINDOW_DAYS       = 7              # only for prune delivery-velocity + geo/weekly views
CREATIVE_BC_GATE = 5             # LIFETIME bc to be efficiency-killable
AGE_GRACE_DAYS    = 7             # v2.3.0: below CREATIVE_BC_GATE is fine within the first 7d
                                    # of deployment; past that, still-thin + bad CPBC + under the
                                    # brake spend floor becomes kill-eligible (closes the case where
                                    # a creative never crosses either gate and sits in MONITOR forever)
ZERO_BC_SPEND    = 10000         # Rs lifetime spend, 0 bc -> kill
KILL_MULT         = {'L1': 1.0, 'L2': 1.0, 'L3': 1.2, 'untagged': 1.0}
DAILY_KILL_CAP    = 3             # v2.2.0: if efficiency-kill candidates > 3, rank by ratio worst-first, cap at 3
TOP_SPENDER_SHARE = 0.10          # v2.2.0: warn (not block) if kill candidate holds >10% of pool daily avg spend
L3_FLIP           = False         # Discovery box not operational -> L3 holds 1.2x (auto-flip to 1.0 later)
BLENDED_TARGET    = 500           # Rs; C* = BLENDED_TARGET * totalBC / paidBC
BRAKE_SPEND_FLOOR = 15000         # Rs
BRAKE_CSTAR_MULT  = 5
BRAKE_CPBC_MULT  = 2.0           # x the creative kill line
ISOLATE_MULT      = 0.7
ISOLATE_BC_GATE  = 12
POOL_CAP          = 15
# v2.7.0-experimental (2026-07-30, Nikhil): NOT the production default (still lifetime -
# see decide()'s variant='lifetime' default). This is an opt-in variant ('l7d') under
# observation via l7d_diff_pass.py: efficiency kill judged on L7-day CPBC against an L7D
# median, instead of lifetime CPBC against a lifetime median - not just swapping the
# benchmark while still judging lifetime CPBC against it (that pairing is incoherent:
# lifetime is a slow-moving average partly built on this account's cheaper early-July
# history, an L7D median reflects today's pricier reality, so almost everything would look
# artificially cheap against a bar it hasn't caught up to yet). Metric and benchmark are
# both L7D together, so the comparison stays apples-to-apples. CREATIVE_BC_GATE (lifetime)
# still gates overall kill-eligibility (is this an established creative at all) even under
# this variant; AGE_GRACE_DAYS and the aged-out path are untouched in both variants
# (deliberately still lifetime-based - a thin, still-ramping creative's L7 window is even
# noisier than its already-thin lifetime number, not an improvement).
#
# L7_MEDIAN_BC_GATE = 1, not a quality bar: every currently active creative should count
# toward the L7 median (Nikhil, 2026-07-30) - a higher gate (tried 2) would exclude exactly
# the case this variant is meant to catch fast: a newer ad with decent spend and zero recent
# bookings. That creature still gets JUDGED regardless of this gate (unaffected - it always
# has a well-defined lifetime bc via CREATIVE_BC_GATE, and cpbc_l7() correctly returns inf for
# it, which is > any finite median). The =1 floor here is a mathematical floor, not a
# judgment call: a creature with ZERO L7 bookings has no defined L7 CPBC (division by zero),
# so it literally cannot be averaged into the median - not a policy choice to exclude it.
L7_MEDIAN_BC_GATE = 1             # min L7-window bookings to count toward the L7 median -
                                    # the mathematical floor (need >=1 booking for a ratio to
                                    # exist at all), not a quality/noise-reduction threshold
MATURE_GEOS       = {'Delhi'}
GEO_BUDGET_BC_GATE = 10
GEO_CONV_INSTALLS = 100
GEO_CONV_MULT     = 2.0
GEO_RUNAWAY_SPEND = 50000
DASH_BASE         = 'https://growth-portal.up.railway.app'
ADS_MANAGER       = 'https://adsmanager.facebook.com/adsmanager/manage/ads'
CONCEPT_RE        = re.compile(r'(?:JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)\d{2}-[TCRH]-\d{3}')
# was hardcoded to 'JUN26-' only (2026-07-30, Nikhil caught it): every JUL26-prefixed
# creative (5 new ones, added while this stayed JUN26-only) was completely invisible to
# meta_active_del(), compute(), and the Activity Log parser - never got a verdict, never
# counted toward the pool or the median, never eligible for the reactivation grace period.
# Month-agnostic now so this doesn't silently recur every time a new month's batch launches.
SLACK_CHANNEL_DEFAULT = 'C0B9G0Q68G6'   # #growth-reports
SLACK_DM_DEFAULT      = 'U05A9037VFG'   # Nikhil
META_ACC_DEFAULT      = '2007675312900454'
META_VER_DEFAULT      = 'v23.0'

# ---- logging constants ----
LOG_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kill_pass_log.json')
ACTION_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kill_action_log.csv')
SHEET_ID        = '145hcZtsX_W-ibI5SrW9tksVO0J-Tka9NtuIaIpZglqA'
SHEET_TAB       = 'Recos'

# ---- reactivation-window state (v2.5.0, 2026-07-23) ----
# Nikhil, 2026-07-23: a creative reactivated after a genuine pause was being
# re-killed the next day off its stale PRE-PAUSE lifetime CPBC, with zero
# chance to post fresh performance first (the same numbers that likely got it
# paused in the first place). Fix: "lifetime" for kill-eligibility purposes
# means since the LATER of first-ever-spend or last genuine reactivation, not
# always since first-ever-spend. One rule for everyone - a creative that's
# never been paused has last-activation == first-spend, so nothing changes
# for it. A same-day pause+unpause doesn't count as a reset (must span a
# distinct earlier calendar date) - guards against trivially resetting the
# clock by toggling status back within the same day.
ACTIVATION_STATE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creative_activation_state.json')

# ---- daily status-snapshot backstop (v2.8.0, 2026-08-08) ----
# Confirmed directly (Nikhil manually unpaused JUN26-T-048/T-050 on 2026-08-05): the
# Activity Log CAN silently miss a real status transition. update_ad_run_status fired
# normally for 75 other ads in that same window, and update_ad_set_run_status fired for
# 4 unrelated ad sets - but zero events at all for these two ads, their ad set, or their
# campaign. Both got killed the next day on lifetime CPBC dating back to their original
# June 15 launch, because the reactivation-window reset never fired. This is an
# independent, always-on backstop that doesn't depend on the Activity Log at all: it
# just diffs today's live active set against whatever was last snapshotted. Detects the
# same class of event by a completely different mechanism, so a gap in one doesn't
# silently propagate through the other.
STATUS_SNAPSHOT_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creative_status_snapshot.json')

# ---- manual last-activation override (v2.9, 2026-08-29) ----
# Confirmed live (JUN26-T-064): the Activity Log can miss BOTH the pause and the
# reactivation of a cycle entirely, not just one side of it - its last logged event
# already said 'Active' (from 2026-07-28), so there's no pause-then-reactivate PAIR to
# derive a fresh date from, even though a real pause and a real reactivation both
# happened in between. Synthesizing a fake 'Inactive' event with a made-up date to force
# the pairing would fabricate history - refused deliberately, don't do that. This override
# instead states the known-true fact directly: {concept_id: 'YYYY-MM-DD'}, wins over
# whatever the log derives, same pattern as budget_shift_pass.py's ad_set_age_overrides.json.
ACTIVATION_OVERRIDE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'creative_activation_overrides.json')


def _load_activation_overrides():
    if os.path.exists(ACTIVATION_OVERRIDE_PATH):
        try:
            with open(ACTIVATION_OVERRIDE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def load_env():
    path = r'C:\credentials\.env'
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); k = k.strip()
                if k not in os.environ: os.environ[k] = v.strip().strip('"').strip("'")


def dget(path):
    req = urllib.request.Request(DASH_BASE + path,
        headers={'X-Dashboard-Token': os.environ['WIOM_DASHBOARD_TOKEN'], 'User-Agent': 'wiom-rule-pass'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def geo_of(name):
    n = (name or '').upper()
    if 'DEL' in n or 'DELHI' in n: return 'Delhi'
    if 'BHARAT' in n or 'BHA' in n: return 'Bharat'
    if 'MUM' in n or 'MUMBAI' in n: return 'Mumbai'
    return None


def layer_of(name):
    m = re.search(r'_(L[0-3])_', name or ''); return m.group(1) if m else 'untagged'


def need_of(name):
    parts = (name or '').split('_')
    for i, p in enumerate(parts):
        if re.fullmatch(r'L[0-3]', p) and i + 1 < len(parts): return parts[i + 1]
    return '?'


def meta_active_del():
    """Active BOOKNOW DEL BFC-VOLUME concepts from Meta.
    Returns (active_set_or_None, ad_ids_map).
    active_set: set of concept_ids. None if Meta unavailable (degraded mode).
    ad_ids_map: {concept_id: [ad_id, ...]} for logging.
    """
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok: return None, {}
    acc = os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)
    if not str(acc).startswith('act_'): acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    active = set()
    ad_ids_map = collections.defaultdict(list)
    calls = 0
    url = f'https://graph.facebook.com/{ver}/{acc}/ads?' + urllib.parse.urlencode(
        {'fields': 'id,name,effective_status,adset{name},campaign{name}', 'limit': 500, 'access_token': tok})
    try:
        while url and calls < 25:
            with urllib.request.urlopen(url, timeout=90) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: Meta active-filter unavailable ->', j['error'].get('message')); return None, {}
            for a in j.get('data', []):
                if a.get('effective_status') != 'ACTIVE': continue
                nm = a.get('name', '') or ''
                camp = ((a.get('campaign') or {}).get('name') or '').upper()
                aset = ((a.get('adset') or {}).get('name') or '').upper()
                if 'BFC-VOLUME' not in camp or 'BOOKNOW' not in nm.upper() or 'DEL' not in aset: continue
                m = CONCEPT_RE.search(nm)
                if m:
                    cid = m.group(0)
                    active.add(cid)
                    if a.get('id'): ad_ids_map[cid].append(a['id'])
            calls += 1
            url = (j.get('paging') or {}).get('next')
        return active, dict(ad_ids_map)
    except Exception as e:
        body = ''
        try: body = e.read().decode()[:160]
        except Exception: pass
        print('warn: Meta active-filter unavailable ->', str(e)[:80], body)
        return None, {}


def meta_today_spend(ad_ids_map, today_date):
    """Live spend-SO-FAR TODAY (not the D-1 anchor decide() judges everything on) per
    concept_id. Added 2026-08-17 (Nikhil): JUN26-T-048 was recommended KILL off D-1
    lifetime CPBC, but by the time of the run it had already become the DEL_ALL_PBFC
    pool's #1 live spender today - a same-day surge the once-daily D-1 pass structurally
    cannot see (unlike the top-spender check, which sees it once it shows up in a
    trailing-7d window, i.e. a day or more later). This is a read-only cross-check, not
    a verdict change - decide() and the KILL list are untouched; it only adds a warning
    label so a human doesn't pause something that's actively becoming the account's
    biggest spender right now. Returns {concept_id: spend} for concepts with any spend
    today; empty dict (silently) if Meta is unavailable, so a failure here never blocks
    the rest of the pass."""
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok or not ad_ids_map: return {}
    acc = os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)
    if not str(acc).startswith('act_'): acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    ad_to_concept = {aid: cid for cid, ids in ad_ids_map.items() for aid in ids}
    spend_by_ad = {}
    url = f'https://graph.facebook.com/{ver}/{acc}/insights?' + urllib.parse.urlencode({
        'level': 'ad', 'fields': 'ad_id,spend',
        'time_range': json.dumps({'since': today_date.isoformat(), 'until': today_date.isoformat()}),
        'limit': 500, 'access_token': tok})
    calls = 0
    try:
        while url and calls < 10:
            with urllib.request.urlopen(url, timeout=60) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: live-spend check unavailable ->', j['error'].get('message')); return {}
            for row in j.get('data', []):
                spend_by_ad[row['ad_id']] = float(row.get('spend', 0))
            calls += 1
            url = (j.get('paging') or {}).get('next')
    except Exception as e:
        print(f'warn: live-spend check failed - {str(e)[:120]}'); return {}
    spend_by_concept = collections.defaultdict(float)
    for aid, sp in spend_by_ad.items():
        cid = ad_to_concept.get(aid)
        if cid: spend_by_concept[cid] += sp
    return dict(spend_by_concept)


def meta_ad_status_check(ad_ids, reco_date_str, ver, tok):
    """Check if ads for a concept were paused on reco_date (IST).
    Uses configured_status + updated_time (IST conversion).
    Returns (action_taken: 'Yes'|'No', action_timing: 'On-time'|'Late'|None).
    """
    reco_date = datetime.date.fromisoformat(reco_date_str)
    for ad_id in ad_ids:
        url = (f'https://graph.facebook.com/{ver}/{ad_id}?'
               f'fields=configured_status,effective_status,updated_time'
               f'&access_token={tok}')
        try:
            with urllib.request.urlopen(url, timeout=30) as r:
                a = json.loads(r.read().decode())
            if a.get('configured_status') == 'PAUSED':
                ut = a.get('updated_time', '')
                timing = None
                if ut:
                    # Meta returns "2026-06-26T10:30:00+0000" - parse as UTC, convert to IST
                    ut_utc = datetime.datetime.strptime(ut[:19], '%Y-%m-%dT%H:%M:%S')
                    ut_ist = ut_utc + datetime.timedelta(hours=5, minutes=30)
                    timing = 'On-time' if ut_ist.date() == reco_date else 'Late'
                return 'Yes', timing or 'Unknown'
        except Exception:
            continue
    return 'No', None


# ---- log read / write ----

def load_log():
    if os.path.exists(LOG_PATH):
        try:
            with open(LOG_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f'warn: could not read log - {e}')
    return []


def save_log(log):
    try:
        with open(LOG_PATH, 'w', encoding='utf-8') as f:
            json.dump(log, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f'warn: could not write log - {e}')


def retro_check(log, d1, still_flagged_today=None):
    """Back-fill action_taken for yesterday's KILL entries. Returns list of unacted recos.
    Top-spender-flagged kills are excluded from unacted (2026-07-23) - scaling a replacement
    before pausing is the whole point of that flag, so it not being paused yet isn't a miss.
    action_taken/action_timing are still back-filled for these (record-keeping, Sheet sync),
    only the Slack nag is suppressed.

    still_flagged_today (2026-08-18, Nikhil - generalized from the narrower 2026-08-15
    top-spender/live-spend fix): concept_ids that TODAY's fresh run still considers
    over-threshold in ANY form - res['kills'] + deferred_kills (capped) +
    deferred_top_spender + deferred_live_spend. "Not acted upon" only makes sense if the
    concept would still be flagged if evaluated fresh today; it may not be, for reasons
    that have nothing to do with the creative itself - e.g. JUN26-T-064 (2026-08-18):
    same CPBC two days running (~Rs1,097), genuinely over yesterday's median (Rs1,093)
    but now under today's median (Rs1,117) purely because the POOL's median drifted up,
    not because T-064 changed. The bar moved, not the creative. If a concept isn't
    anywhere in today's still_flagged_today set - not even PROTECTED - today's numbers
    don't call it a problem, so nagging about not pausing it yesterday is stale and
    should be dropped, same principle as the top-spender/live-spend cases, just not
    limited to those two specific reasons anymore."""
    yesterday = (d1 - datetime.timedelta(days=1)).isoformat()
    tok = os.environ.get('META_ACCESS_TOKEN')
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    # None means "no info about today's set was passed" - fail toward the old, safe
    # default (nag on everything) rather than silently suppressing all nags. Only an
    # explicit set (even an empty one, from a real res) enables the smarter filter.
    filter_by_today = still_flagged_today is not None
    still_flagged_today = still_flagged_today or set()
    unacted = []
    for entry in log:
        if entry['date'] != yesterday: continue
        for reco in entry.get('recos', []):
            if reco.get('verdict') != 'KILL': continue
            if reco.get('action_taken') is None:
                ad_ids = reco.get('ad_ids', [])
                if not tok or not ad_ids:
                    reco['action_taken'] = 'Unknown'
                    continue
                taken, timing = meta_ad_status_check(ad_ids, yesterday, ver, tok)
                reco['action_taken'] = taken
                reco['action_timing'] = timing
            if reco.get('action_taken') != 'No' or reco.get('top_spender'):
                continue
            if filter_by_today and reco.get('concept_id') not in still_flagged_today:
                continue
            unacted.append(reco)
    return unacted


def write_log_entry(log, res, d1, ad_ids_map, median):
    """Append today's KILL recos to the log (idempotent - replaces any existing entry for d1)."""
    recos = []
    top_spenders = res.get('top_spender_warns', set())
    for (c, lyr, need, lb, sp, x, reason) in res['kills']:
        recos.append({
            'concept_id': c,
            'layer': lyr,
            'need': need,
            'verdict': 'KILL',
            'reason': reason,
            'cpbc': round(x, 2) if x != float('inf') else None,
            'bc': int(lb),
            'spend': round(sp, 2),
            'median_at_time': round(median, 2) if median else None,
            'top_spender': c in top_spenders,
            'ad_ids': ad_ids_map.get(c, []),
            'action_taken': None,
            'action_timing': None,
        })
    log = [e for e in log if e['date'] != d1.isoformat()]
    log.append({
        'date': d1.isoformat(),
        'median_cpbc': round(median, 2) if median else None,
        'recos': recos,
    })
    log.sort(key=lambda e: e['date'])
    return log


# ---- Google Sheets sync ----

def sheet_sync(log, d1):
    """Append today's reco rows + update yesterday's action_taken columns in the Sheet."""
    try:
        import gspread
        gc = gspread.oauth(
            credentials_filename=r'C:\Users\nikhi\.config\gspread\credentials.json',
            authorized_user_filename=r'C:\Users\nikhi\.config\gspread\authorized_user.json',
        )
        ws = gc.open_by_key(SHEET_ID).worksheet(SHEET_TAB)
        today_str = d1.isoformat()
        yesterday_str = (d1 - datetime.timedelta(days=1)).isoformat()

        # Append today's rows
        for entry in log:
            if entry['date'] != today_str: continue
            for reco in entry.get('recos', []):
                x = reco.get('cpbc')
                med = reco.get('median_at_time')
                ws.append_row([
                    entry['date'],
                    reco['concept_id'],
                    reco['verdict'],
                    reco.get('reason', ''),
                    f"{x:,.0f}" if x is not None else 'inf',
                    reco.get('bc', ''),
                    f"{reco.get('spend', 0):,.0f}",
                    f"{med:,.0f}" if med is not None else '',
                    ','.join(reco.get('ad_ids', [])),
                    '',   # action_taken - filled next day
                    '',   # action_timing - filled next day
                ], value_input_option='USER_ENTERED')

        # Back-fill action_taken / action_timing for yesterday's rows
        all_vals = ws.get_all_values()
        for i, row in enumerate(all_vals[1:], start=2):
            if len(row) < 10 or row[0] != yesterday_str: continue
            if row[9] not in ('', None): continue   # already filled
            concept = row[1]
            for entry in log:
                if entry['date'] != yesterday_str: continue
                for reco in entry.get('recos', []):
                    if reco['concept_id'] == concept and reco.get('action_taken') is not None:
                        ws.update(f'J{i}:K{i}', [[reco['action_taken'], reco.get('action_timing') or '']])
        print('Sheet sync done')
    except ImportError:
        print('warn: gspread not installed - skipping Sheet sync (pip install gspread)')
    except Exception as e:
        print(f'warn: Sheet sync failed - {e}')


def write_action_log_csv(log, d1):
    """Append yesterday's back-filled KILL recos to kill_action_log.csv."""
    import csv
    yesterday = (d1 - datetime.timedelta(days=1)).isoformat()
    rows = []
    for entry in log:
        if entry['date'] != yesterday: continue
        for reco in entry.get('recos', []):
            if reco.get('verdict') != 'KILL': continue
            if reco.get('action_taken') is None: continue  # retro check didn't run / Meta unavailable
            rows.append([
                entry['date'],
                reco.get('concept_id', ''),
                reco.get('layer', ''),
                reco.get('need', ''),
                reco.get('bc', ''),
                reco.get('spend', ''),
                reco.get('cpbc', ''),
                reco.get('median_at_time', ''),
                reco.get('reason', ''),
                '|'.join(reco.get('ad_ids', [])),
                reco.get('action_taken', ''),
                reco.get('action_timing', ''),
            ])
    if not rows: return
    try:
        write_header = not os.path.exists(ACTION_LOG_PATH) or os.path.getsize(ACTION_LOG_PATH) == 0
        with open(ACTION_LOG_PATH, 'a', newline='', encoding='utf-8') as f:
            w = csv.writer(f)
            if write_header:
                w.writerow(['reco_date', 'concept_id', 'layer', 'need_state', 'bc', 'spend',
                            'cpbc', 'median_at_time', 'reason', 'ad_ids',
                            'action_taken', 'action_timing'])
            w.writerows(rows)
        print(f'action log: wrote {len(rows)} row(s) for {yesterday}')
    except Exception as e:
        print(f'warn: could not write action log - {e}')


def git_commit_log(d1):
    """Commit the updated log JSON (+ activation-state cache) back to the repo."""
    try:
        repo = os.path.dirname(os.path.abspath(__file__))
        paths = [LOG_PATH, ACTION_LOG_PATH]
        if os.path.exists(ACTIVATION_STATE_PATH): paths.append(ACTIVATION_STATE_PATH)
        if os.path.exists(STATUS_SNAPSHOT_PATH): paths.append(STATUS_SNAPSHOT_PATH)
        subprocess.run(['git', 'add'] + paths, cwd=repo, check=True, capture_output=True)
        result = subprocess.run(
            ['git', 'commit', '-m', f'kill-pass log {d1.isoformat()}'],
            cwd=repo, capture_output=True
        )
        if result.returncode == 0:
            print('log committed to git')
        elif b'nothing to commit' in result.stdout + result.stderr:
            print('log: nothing new to commit')
        else:
            print(f'warn: git commit failed - {result.stderr.decode()[:120]}')
    except Exception as e:
        print(f'warn: git commit failed - {e}')


def _load_status_snapshot():
    if os.path.exists(STATUS_SNAPSHOT_PATH):
        try:
            with open(STATUS_SNAPSHOT_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return None


def _save_status_snapshot(d1, active_now):
    try:
        with open(STATUS_SNAPSHOT_PATH, 'w', encoding='utf-8') as f:
            json.dump({'date': d1.isoformat(), 'active_concepts': sorted(active_now)}, f, indent=2)
    except Exception as e:
        print(f'warn: could not write status snapshot - {e}')


def _snapshot_diff_reactivations(d1, active_now):
    """Backstop reactivation detector - see STATUS_SNAPSHOT_PATH note above for why this
    exists alongside (not instead of) the Activity-Log-based detection. Compares today's
    live active set against whatever was last snapshotted (could be >1 day old if a run
    was skipped - e.g. this pass was disabled for several days around 2026-08-06/07). Any
    concept active now but absent from the last snapshot is treated as reactivated as of
    TODAY (d1) - the safe upper bound, since we only know it flipped SOMETIME between the
    two snapshots, not exactly when. First-ever run (no snapshot on disk yet) seeds
    silently and reports zero reactivations - a cold start must not treat the entire live
    pool as freshly reactivated. active_now=None (Meta unavailable) skips entirely -
    never snapshot on incomplete data, or the next real comparison would falsely see
    everything as a new reactivation."""
    if active_now is None:
        return {}
    prev = _load_status_snapshot()
    reactivated = {}
    if prev is not None:
        prev_active = set(prev.get('active_concepts', []))
        for cid in active_now:
            if cid not in prev_active:
                reactivated[cid] = d1.isoformat()
    _save_status_snapshot(d1, active_now)
    return reactivated


def _load_activation_state():
    if os.path.exists(ACTIVATION_STATE_PATH):
        try:
            with open(ACTIVATION_STATE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {'last_checked': None, 'events': {}}


def _save_activation_state(state):
    try:
        with open(ACTIVATION_STATE_PATH, 'w', encoding='utf-8') as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception as e:
        print(f'warn: could not write activation state - {e}')


def _fetch_activity_events(since, until):
    """Pull ad status-change events from Meta's account Activity Log for [since, until],
    filtered to BFC-VOLUME BOOKNOW concepts. Returns {concept_id: [{'date','from','to'}, ...]}.
    Read-only, account-wide edge (not filterable by campaign) - client-side filtered same as
    everywhere else in this script. Bounded to 30 pages (~3,000 rows) as a sane cap; a full
    Jun 1 backfill needs ~15 pages in practice."""
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok: return {}
    acc = os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)
    if not str(acc).startswith('act_'): acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    events = collections.defaultdict(list)
    params = {
        'since': since, 'until': until, 'limit': 100,
        'fields': 'event_type,event_time,object_name,extra_data',
        'access_token': tok,
    }
    url = f'https://graph.facebook.com/{ver}/{acc}/activities?' + urllib.parse.urlencode(params)
    pages = 0
    try:
        while url and pages < 30:
            with urllib.request.urlopen(url, timeout=60) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: activity log unavailable ->', j['error'].get('message'))
                return {}
            for row in j.get('data', []):
                if row.get('event_type') != 'update_ad_run_status': continue
                nm = row.get('object_name') or ''
                m = CONCEPT_RE.search(nm)
                if not m or 'BOOKNOW' not in nm.upper(): continue
                cid = m.group(0)
                try:
                    extra = json.loads(row.get('extra_data') or '{}')
                except Exception:
                    continue
                old_v = str(extra.get('old_value', '')); new_v = str(extra.get('new_value', ''))
                et = row.get('event_time', '')
                try:
                    ts = datetime.datetime.strptime(et[:19], '%Y-%m-%dT%H:%M:%S')
                except Exception:
                    continue
                # keep the full timestamp, not just the date - same-day events (e.g. an
                # ad's initial Meta review pipeline churning through several states within
                # minutes of creation) need TRUE chronological order to resolve correctly,
                # not just date order (2026-07-23: a same-day-only sort mis-resolved
                # T-063's creation-day churn as a false later "reactivation").
                events[cid].append({'ts': ts.isoformat(), 'date': ts.date().isoformat(), 'from': old_v, 'to': new_v})
            pages += 1
            url = (j.get('paging') or {}).get('next')
    except Exception as e:
        print(f'warn: activity log fetch failed - {str(e)[:120]}')
        return {}
    return dict(events)


def _derive_last_activation(events_for_concept):
    """From chronological status-change events for one concept, find the most recent
    reactivation (-> Active) whose preceding pause (-> Inactive) spanned a distinct
    earlier calendar date (not a same-day pause+unpause - that's not treated as a
    reset). This account's activity log uses 'Active'/'Inactive' as the terminal
    states, not literally 'Paused' - 'Pending process'/'Pending Review' are transient
    review-queue noise around either transition and are ignored here."""
    evs = sorted(events_for_concept, key=lambda e: e.get('ts', e['date']))
    last_inactive_date = None
    last_activation = None
    for e in evs:
        to = e['to']
        if to == 'Inactive':
            last_inactive_date = e['date']
        elif to == 'Active':
            if last_inactive_date and last_inactive_date != e['date']:
                last_activation = e['date']
            last_inactive_date = None
    return last_activation


def get_last_activation_dates(d1, active_now=None):
    """Per-concept last-activation date (see ACTIVATION_STATE_PATH note above). Cached to
    disk and refreshed incrementally (only new events since last check) - a full backfill
    to CAMPAIGN_START only happens once, the first time this runs.

    active_now (v2.8.0): today's live active set, passed in so the snapshot-diff backstop
    (see STATUS_SNAPSHOT_PATH) can run alongside the Activity-Log-based detection - two
    independent mechanisms for the same fact, unioned below so a gap in one doesn't
    silently propagate through. Callers that don't pass active_now just get the
    Activity-Log-only behavior (backstop skips itself, same as active_now=None)."""
    state = _load_activation_state()
    last_checked = state.get('last_checked')
    since = CAMPAIGN_START if not last_checked else last_checked
    new_events = _fetch_activity_events(since, d1.isoformat())
    events = state.setdefault('events', {})
    for cid, evs in new_events.items():
        existing = events.setdefault(cid, [])
        seen = {(e['date'], e['from'], e['to']) for e in existing}
        for e in evs:
            key = (e['date'], e['from'], e['to'])
            if key not in seen:
                existing.append(e); seen.add(key)
    state['last_checked'] = d1.isoformat()
    _save_activation_state(state)
    from_log = {cid: d for cid, evs in events.items() if (d := _derive_last_activation(evs))}

    from_snapshot = _snapshot_diff_reactivations(d1, active_now)
    # from_log wins whenever it has a date - it's the precise, authoritative event date.
    # from_snapshot only fills genuine gaps (concepts the log has nothing for at all) -
    # it must NOT compete by "later date wins", or a snapshot's approximate first-noticed
    # date (which can lag the real event by days if a run was skipped) would override a
    # correct, more precise date the log already has. Confirmed live 2026-08-08: the log
    # eventually got the JUN26-T-048/T-050 event (dated 2026-08-05, correct) days after
    # this pass first needed it - so this isn't hypothetical, it's the exact ordering that
    # would otherwise silently corrupt a date the log later resolves correctly.
    #
    # EXCEPTION (2026-08-25, JUN26-C-073 incident): the above assumes from_log, when it has
    # a date, is simply "not yet caught up" - but it can instead be flat-out stale: the
    # log's own last known event for a concept can end on a pause (-> Inactive), i.e. the
    # log itself believes the concept is currently paused, while active_now says it's live.
    # That's not a precision gap, it's a missed event entirely - a brake/kill decision built
    # on that from_log date would be judging the concept's spend/CPBC from its PRIOR
    # activation cycle (here, June) instead of its real one. Detect that specific
    # contradiction and fall through to from_snapshot for just those concepts, same as a
    # genuine gap - this does not touch the 2026-08-08 case at all, since there the log's
    # last known state was consistent with being active (no contradiction), just late.
    def _log_thinks_inactive(cid):
        evs = sorted(events.get(cid, []), key=lambda e: e.get('ts', e['date']))
        return bool(evs) and evs[-1]['to'] != 'Active'
    stale_log = {cid for cid in from_log if active_now and cid in active_now and _log_thinks_inactive(cid)}
    if stale_log:
        print(f'log has a stale last-activation date (log thinks paused, live is active) for {len(stale_log)}: {sorted(stale_log)}')
    merged = {cid: d for cid, d in from_log.items() if cid not in stale_log}
    for cid, d in from_snapshot.items():
        if cid not in merged:
            merged[cid] = d
    newly_caught = set(from_snapshot) - set(from_log)
    if newly_caught:
        print(f'snapshot-diff caught {len(newly_caught)} reactivation(s) the Activity Log missed: {sorted(newly_caught)}')

    overrides = _load_activation_overrides()
    if overrides:
        print(f'manual activation-date override applied for {len(overrides)}: {sorted(overrides)}')
        merged.update(overrides)
    return merged


def compute(d1, last_activation=None):
    last_activation = last_activation or {}
    metric_start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = dget('/api/master_export?' + urllib.parse.urlencode({'start': CAMPAIGN_START, 'end': d1.isoformat()}))
    # pass 1: raw per (geo, cid) rows, so each concept's true first-spend date is known
    # before deciding what counts as its "lifetime" window (see reactivation-window note).
    raw = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if r.get('channel') != 'META' or 'BFC-VOLUME' not in str(r.get('campaign', '')).upper(): continue
        nm = str(r.get('creative', ''))
        if 'BOOKNOW' not in nm.upper(): continue
        m = CONCEPT_RE.search(nm)
        if not m: continue
        cid = m.group(0); g = geo_of(r.get('ad_set', '')) or geo_of(r.get('campaign', '')) or 'Other'
        dt = str(r.get('date', '')); sp = r.get('spend') or 0; bf = r.get('booking_confirmed') or 0; ins = r.get('app_installs') or 0
        raw[g][cid].append((dt, sp, bf, ins, layer_of(nm), need_of(nm)))

    first = {}
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            spent_dates = [dt for dt, sp, bf, ins, lyr, need in rws if sp > 0]
            if spent_dates:
                d0 = min(spent_dates)
                if cid not in first or d0 < first[cid]: first[cid] = d0

    window_start = {}
    for cid, d0 in first.items():
        la = last_activation.get(cid)
        window_start[cid] = max(d0, la) if la else d0

    data = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'spend': 0.0, 'bc': 0, 'inst': 0, 'w7s': 0.0, 'w7i': 0, 'layer': 'untagged', 'need': '?'}))
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            wstart = window_start.get(cid) or first.get(cid, '')
            rec = data[g][cid]
            for dt, sp, bf, ins, lyr, need in rws:
                rec['layer'] = lyr; rec['need'] = need
                if dt >= wstart:
                    rec['spend'] += sp; rec['bc'] += bf; rec['inst'] += ins
                if dt >= metric_start:
                    rec['w7s'] += sp; rec['w7i'] += ins; rec['w7b'] = rec.get('w7b', 0) + bf
    # ---- funnel rows for non-Delhi geo diagnostic (7-day window) ----
    funnel_geo = collections.defaultdict(lambda: {'inst': 0, 'svc_check': 0, 'svc_true': 0, 'bc': 0, 'w7s': 0.0})
    try:
        frows = dget('/api/funnel_rows?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        for r in frows:
            aset = str(r.get('ad_set', '')); camp = str(r.get('campaign', ''))
            g = geo_of(aset) or geo_of(camp)
            if not g: continue
            fg = funnel_geo[g]
            fg['inst']      += r.get('app_installs') or 0
            fg['svc_check'] += r.get('serviceable_check') or 0
            fg['svc_true']  += r.get('serviceable_true') or 0
            fg['bc']       += r.get('booking_confirmed') or 0
        # pull 7-day spend per non-Delhi geo from master_export (already iterated above via data)
        for g, wc in data.items():
            if g == 'Other': continue
            funnel_geo[g]['w7s'] = sum(x['w7s'] for x in wc.values())
    except Exception as e:
        print(f'warn: funnel_rows fetch failed - {e}')
    # age keys off window_start (not first-ever-spend) so a reactivated creative gets the
    # same AGE_GRACE_DAYS runway as a brand-new one, instead of appearing "aged out" (past
    # grace) while its fresh-window bookings are still thin - see reactivation-window note.
    age = {}
    for cid, ds in window_start.items():
        try: age[cid] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception: age[cid] = 999
    cstar = None
    try:
        wr = dget('/api/war_room?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        days = wr.get('days', wr) if isinstance(wr, dict) else wr
        tot = sum(d.get('bookings') or 0 for d in days)
        paid = sum((d.get('meta_bfc') or 0) + (d.get('google_bfc') or 0) for d in days)  # external growth-dashboard field names - NOT renamed
        if paid: cstar = BLENDED_TARGET * tot / paid
    except Exception: pass
    return data, age, cstar, dict(funnel_geo)


def cpbc(rec): return rec['spend'] / rec['bc'] if rec['bc'] else float('inf')
def cpbc_l7(rec):
    b = rec.get('w7b', 0)
    return rec['w7s'] / b if b else float('inf')


def decide(data, age, cstar, active, funnel_geo=None, variant='lifetime'):
    """variant='lifetime' (default, production): efficiency judged on lifetime CPBC vs a
    lifetime median - exactly the committed v2.6.0 behavior, unchanged.
    variant='l7d' (experimental, under observation via l7d_diff_pass.py - NOT wired into
    the daily production run): efficiency judged on L7-day CPBC vs an L7-day median instead
    - see the L7_MEDIAN_BC_GATE comment block above for why. Everything else (zero-BC kill,
    cost-velocity brake, top-spender protection, aged-out path, pool-cap prune, weekly
    geo checks) is identical in both variants - only the median basis and the per-creature
    judged metric change."""
    res = {'kills': [], 'reviews': [], 'isolates': [], 'prune_cut': [], 'pool_n': 0, 'continue': 0,
           'monitor': 0, 'median': None, 'brake_spend': None, 'geo_budget': [], 'geo_conv': [],
           'active_filter': active is not None}
    pool = data.get('Delhi', {})
    spent = [c for c in pool if pool[c]['spend'] > 0]
    def act(c): return (active is None) or (c in active)
    if variant == 'l7d':
        elig = [c for c in spent if act(c) and pool[c].get('w7b', 0) >= L7_MEDIAN_BC_GATE]
        med = statistics.median([cpbc_l7(pool[c]) for c in elig]) if elig else None
    else:
        elig = [c for c in spent if act(c) and pool[c]['bc'] >= CREATIVE_BC_GATE]
        med = statistics.median([cpbc(pool[c]) for c in elig]) if elig else None
    res['median'] = med
    brake_spend = max(BRAKE_CSTAR_MULT * cstar, BRAKE_SPEND_FLOOR) if cstar else BRAKE_SPEND_FLOOR
    res['brake_spend'] = brake_spend
    verdict = {}
    eff_kill_candidates = []  # (c, lyr, need, lb, sp, x, reason, ratio) - capped below
    for c in spent:
        if not act(c): continue
        rec = pool[c]; lb = rec['bc']; sp = rec['spend']; x = cpbc(rec); x_l7 = cpbc_l7(rec); lyr = rec['layer']
        judged = x_l7 if variant == 'l7d' else x
        mult = KILL_MULT.get(lyr, 1.0)
        if lyr == 'L3' and L3_FLIP: mult = 1.0
        kt = (mult * med) if med else None
        if lb == 0 and sp >= ZERO_BC_SPEND:
            verdict[c] = 'KILL'; res['kills'].append((c, lyr, rec['need'], lb, sp, x, 'zero-BC')); continue
        if kt and sp >= brake_spend and x >= BRAKE_CPBC_MULT * kt:
            verdict[c] = 'KILL_REVIEW'; res['reviews'].append((c, lyr, rec['need'], lb, sp, x, f'brake (>=2x line, spend Rs{sp:,.0f})')); continue
        if lb >= CREATIVE_BC_GATE and kt:
            if judged > kt:
                reason = (f'efficiency (L7 CPBC Rs{x_l7:,.0f} > {mult}x L7median Rs{med:,.0f}; lifetime Rs{x:,.0f})'
                          if variant == 'l7d' else f'efficiency (> {mult}x median Rs{med:,.0f})')
                eff_kill_candidates.append((c, lyr, rec['need'], lb, sp, x, reason, judged / med))
                continue
            if judged <= ISOLATE_MULT * med and lb >= ISOLATE_BC_GATE:
                verdict[c] = 'ISOLATE'; res['isolates'].append((c, lyr, rec['need'], lb, sp, x)); continue
            verdict[c] = 'CONTINUE' if judged < med else 'MONITOR'
        elif kt and age.get(c, 0) > AGE_GRACE_DAYS and x > kt:
            # v2.3.0: aged-out - past the 7-day grace window, still below the lifetime
            # BC gate (thin sample) and under the brake spend floor (else the brake
            # check above would already have caught it), but already CPBC-bad enough
            # to fail efficiency if it had reached the gate.
            eff_kill_candidates.append((c, lyr, rec['need'], lb, sp, x,
                f'aged-out (>{AGE_GRACE_DAYS}d, {lb} BC, > {mult}x median Rs{med:,.0f})', x / med))
        else:
            verdict[c] = 'MONITOR'
    # v2.2.0: is this candidate #1 or #2 by 7-day spend AND >10% pool share - moved ahead
    # of the daily-cap loop (v2.6.0) so it can gate kills, not just label them afterward.
    # v2.10 (2026-09-01, Nikhil): raw w7s (a 7-day SUM) unfairly buries a creative that
    # hasn't existed for the full window - confirmed live on AUG26-T-122 (created 2026-08-26
    # per Meta, 5 days old at evaluation): its raw w7s (Rs48,406) ranked #3, but its spend
    # PER ACTIVE DAY (Rs9,681) already beat JUN26-T-063's actual daily rate (Rs54,186/7 =
    # Rs7,741) - T-063 only out-summed it by having 2 more days in the window to accumulate,
    # not by spending faster. That's tenure, not scale, and the protection was built to catch
    # scale. Ranking now uses each candidate's OWN daily rate over its own active days,
    # projected to a full 7-day window (implied_w7s = daily_rate * 7) - a creative running
    # its true pace the whole week would represent this much spend. Denominator
    # (pool_total_w7s) stays the REAL observed pool total, not similarly projected - that
    # keeps the >10% bar meaning "a real dollar share of what the pool actually spent," just
    # slightly harder to clear for a ramping-up creative than an inflated denominator would
    # be, which is the conservative direction for a protection mechanism. age.get(c,7)
    # defaults to 7 (no behavior change) for anything the age dict doesn't cover; floored at
    # 1 to avoid a same-day divide-by-zero; capped at 7 so a creative older than the window
    # doesn't get an artificially deflated daily rate from days outside it - for anything
    # that's already been running the full 7+ days this reduces to the original raw w7s
    # exactly (denominator becomes 7, projected back up by *7 cancels out), so T-048/T-063
    # and every other week-plus-old creative are completely unaffected by this change.
    pool_w7s = {c: pool[c]['w7s'] for c in spent if act(c)}
    pool_total_w7s = sum(pool_w7s.values())
    def _daily_rate(c):
        days = min(max(age.get(c, 7), 1), 7)
        return pool_w7s[c] / days
    implied_w7s = {c: _daily_rate(c) * 7 for c in pool_w7s}
    top2 = sorted(implied_w7s, key=lambda c: -implied_w7s[c])[:2]
    def is_top_spender(c):
        return c in top2 and pool_total_w7s > 0 and implied_w7s[c] / pool_total_w7s > TOP_SPENDER_SHARE

    # v2.2.0: daily kill cap - rank by ratio (worst first), kill top DAILY_KILL_CAP, defer rest to MONITOR
    # v2.6.0 (2026-07-30, Nikhil): a top-spender-flagged candidate is now NEVER auto-killed at
    # all, not just throttled. The original "PROTECTED (find replacement)" design was always
    # advisory - the pass recommended killing it regardless, the flag just reminded the human
    # to scale a replacement first. That's inconsistent with "protected until displaced": if a
    # top-spender is only really safe to remove once something else has scaled up and taken
    # its place, is_top_spender() already re-evaluates that fresh each day from live spend
    # share - once a replacement genuinely displaces it (pushes it out of the top-2, or below
    # 10% share), it stops being flagged and becomes a normal kill candidate again. Until then
    # it's held out of KILL entirely and shown separately, still visible, just not actionable
    # as a pause recommendation. (The cost-velocity brake below is untouched - a top-spender
    # that crosses 2x-the-line still surfaces as KILL_REVIEW, since that's already
    # human-review-only, not automatic.)
    eff_kill_candidates.sort(key=lambda t: -t[7])
    res['deferred_kills'] = []
    res['deferred_top_spender'] = []
    kills_taken = 0
    for (c, lyr, need, lb, sp, x, reason, _ratio) in eff_kill_candidates:
        if is_top_spender(c):
            verdict[c] = 'MONITOR'
            res['deferred_top_spender'].append((c, lyr, need, lb, sp, x, reason))
        elif kills_taken < DAILY_KILL_CAP:
            verdict[c] = 'KILL'
            res['kills'].append((c, lyr, need, lb, sp, x, reason))
            kills_taken += 1
        else:
            verdict[c] = 'MONITOR'
            res['deferred_kills'].append((c, lyr, need, lb, sp, x, reason))
    # top-spender warning label for KILL_REVIEW (brake) entries - kills themselves never
    # contain a top-spender anymore, so this only ever tags reviews now.
    res['top_spender_warns'] = {r[0] for r in res['reviews'] if is_top_spender(r[0])}
    res['continue'] = sum(1 for v in verdict.values() if v == 'CONTINUE')
    res['monitor'] = sum(1 for v in verdict.values() if v == 'MONITOR')
    # ---- pool-cap prune (cap 15, layer x need-state coverage, no per-layer floor) ----
    survivors = [c for c in verdict if verdict[c] not in ('KILL', 'KILL_REVIEW')]
    res['pool_n'] = len(survivors)
    if len(survivors) > POOL_CAP:
        keep = set(c for c in survivors if verdict[c] in ('CONTINUE', 'ISOLATE'))
        monitor = [c for c in survivors if verdict[c] == 'MONITOR']
        cells = collections.defaultdict(list)
        for c in monitor: cells[(pool[c]['layer'], pool[c]['need'])].append(c)
        for _cell, mem in cells.items():
            best = sorted(mem, key=lambda c: (-pool[c]['w7s'], cpbc(pool[c])))[0]; keep.add(best)
        rest = [c for c in monitor if c not in keep]
        ws = [pool[c]['w7s'] for c in rest]
        ineff = [(cpbc(pool[c]) / med if (pool[c]['bc'] and med) else 1.0) for c in rest]
        def z(v, arr):
            mu = sum(arr) / len(arr) if arr else 0
            sd = (statistics.pstdev(arr) if len(arr) > 1 else 1) or 1
            return (v - mu) / sd
        score = {c: z(pool[c]['w7s'], ws) - z((cpbc(pool[c]) / med if (pool[c]['bc'] and med) else 1.0), ineff) for c in rest}
        for c in sorted(rest, key=lambda c: -score[c]):
            if len(keep) < POOL_CAP: keep.add(c)
        res['prune_cut'] = sorted([c for c in survivors if c not in keep], key=lambda c: pool[c]['w7s'])
    # ---- weekly: geo budget (mature geo, 7-day cpbc vs C*) ----
    if cstar:
        for g in MATURE_GEOS:
            wc = data.get(g, {})
            gsp7 = sum(x['w7s'] for x in wc.values()); gbc7 = sum(x.get('w7b', 0) for x in wc.values())
            if gbc7 >= GEO_BUDGET_BC_GATE:
                gcp = gsp7 / gbc7
                res['geo_budget'].append((g, 'SCALE' if gcp <= cstar else 'HOLD', gcp, gbc7))
    # ---- weekly: non-Delhi geo 3-stage diagnostic (7-day spend gate) ----
    fg = funnel_geo or {}
    del_fg = fg.get('Delhi', {}); del_w7s = sum(x['w7s'] for x in data.get('Delhi', {}).values())
    del_svc_check = del_fg.get('svc_check', 0); del_svc_true = del_fg.get('svc_true', 0); del_bc = del_fg.get('bc', 0)
    del_cpsc  = (del_w7s / del_svc_check) if del_svc_check else None
    del_svc_rate  = (del_svc_true / del_svc_check) if del_svc_check else None
    del_conv_rate = (del_bc / del_svc_true) if del_svc_true else None
    for g, gf in fg.items():
        if g in MATURE_GEOS: continue
        w7s = gf.get('w7s', 0)
        if w7s <= 0: continue   # not active in last 7 days
        svc_check = gf.get('svc_check', 0); svc_true = gf.get('svc_true', 0); bc = gf.get('bc', 0)
        if svc_check < GEO_CONV_INSTALLS: continue
        g_cpsc       = w7s / svc_check if svc_check else None
        g_svc_rate   = svc_true / svc_check if svc_check else None
        g_conv_rate  = bc / svc_true if svc_true else None
        flags = []
        if del_cpsc and g_cpsc and g_cpsc > GEO_CONV_MULT * del_cpsc:
            flags.append(f"cost/svc-check Rs{g_cpsc:,.0f} vs Delhi Rs{del_cpsc:,.0f} -> review campaign levers")
        if del_svc_rate and g_svc_rate and g_svc_rate < (1 / GEO_CONV_MULT) * del_svc_rate:
            flags.append(f"svc true% {100*g_svc_rate:.1f}% vs Delhi {100*del_svc_rate:.1f}% -> review targeting lever")
        if del_conv_rate and g_conv_rate and g_conv_rate < (1 / GEO_CONV_MULT) * del_conv_rate:
            flags.append(f"svc->booking {100*g_conv_rate:.1f}% vs Delhi {100*del_conv_rate:.1f}% -> review conversion levers")
        if flags:
            res['geo_conv'].append((g, flags, w7s))
    res['verdict'] = dict(verdict)  # full per-creative classification, for ad-hoc inspection - not used in msg_daily/msg_weekly
    return res


def _row(c, lyr, need, lb, sp, x, reason=None):
    xs = f"{x:,.0f}" if x != float('inf') else 'inf'
    base = f"   - `{c}` [{lyr}/{need}] {int(lb)} BC, Rs{sp:,.0f}, CPBC Rs{xs}"
    return base + (f" - {reason}" if reason else "")


def ads_link():
    acct = str(os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)).replace('act_', '')
    return f"<{ADS_MANAGER}?act={acct}|Open BFC-VOLUME in Ads Manager>"


def integrity_line(res):
    if res.get('active_filter'):
        return "_Integrity: creative active-status vetted live from Meta (effective_status); paused excluded from the median and the lists._"
    return "_Integrity:_ :warning: _active-status NOT vetted from Meta (unavailable) - basis = dashboard spend only, so recently-paused creatives may still appear. Verify in Ads Manager before acting._"


def msg_daily(res, cstar, end, unacted=None):
    kills, reviews, cut = res['kills'], res['reviews'], res['prune_cut']
    integ = integrity_line(res)
    if (not kills and not reviews and not cut and not unacted and not res.get('deferred_kills')
            and not res.get('deferred_top_spender') and not res.get('deferred_live_spend')):
        return f":white_check_mark: *BFC-VOLUME daily kill+prune* ({end}, DEL BOOKNOW, lifetime): no kills, no brake, no prune. Pool {res['pool_n']}/{POOL_CAP}.\n{integ}"
    medlabel = "active-only median" if res['active_filter'] else "median (incl. paused)"
    if res['median']:
        head = (f":scales: *BFC-VOLUME daily kill + prune* ({end}, DEL BOOKNOW, lifetime)\n"
                f"{medlabel} CPBC Rs{res['median']:,.0f} | C* Rs{cstar:,.0f} | brake Rs{res['brake_spend']:,.0f} | pool {res['pool_n']}/{POOL_CAP}\n"
                f"_Decisions for review - read-only, pausing is a manual step in Ads Manager._")
    else:
        head = f":scales: *BFC-VOLUME daily kill + prune* ({end})"
    lines = [head, integ, ""]
    if unacted:
        lines.append(f":warning: *NOT ACTED UPON - yesterday's KILLs still ACTIVE ({len(unacted)})*")
        for reco in unacted:
            x = reco.get('cpbc')
            xs = f"{x:,.0f}" if x is not None else 'inf'
            lines.append(f"   - `{reco['concept_id']}` {reco.get('bc', '?')} BC, CPBC Rs{xs} - still running, pause in Ads Manager")
        lines.append("")
    if kills:
        lines.append(f"*KILL ({len(kills)})*")
        warns = res.get('top_spender_warns', set())
        for k in kills:
            row = _row(*k)
            if k[0] in warns:
                row += "  :warning: *TOP SPENDER - scale replacement before pausing*"
            lines.append(row)
        lines.append("")
    deferred = res.get('deferred_kills', [])
    if deferred:
        lines.append(f"*CAPPED - {len(deferred)} above threshold, deferred to MONITOR (daily cap {DAILY_KILL_CAP})*")
        for k in deferred: lines.append(_row(*k))
        lines.append("")
    deferred_ts = res.get('deferred_top_spender', [])
    deferred_live = res.get('deferred_live_spend', [])
    if deferred_ts or deferred_live:
        lines.append(f"*PROTECTED - {len(deferred_ts) + len(deferred_live)} creative(s) held out of KILL - scale a replacement to displace before pausing*")
        for k in deferred_ts: lines.append(_row(*k))
        for k in deferred_live:
            lines.append(_row(*k) + "  :rotating_light: *today's live spend, not D-1 - already this pool's #1/#2 spender today*")
        lines.append("")
    if reviews:
        lines.append(f"*KILL-REVIEW - cost-velocity brake ({len(reviews)})*  _human look, not auto_")
        warns = res.get('top_spender_warns', set())
        for r in reviews:
            row = _row(*r)
            if r[0] in warns:
                row += "  :warning: *TOP SPENDER - scale replacement before pausing*"
            lines.append(row)
        lines.append("")
    if cut:
        lines.append(f"*PRUNE - pool over cap {POOL_CAP}, cut weakest ({len(cut)})*")
        lines.append("   " + ", ".join(f"`{c}`" for c in cut))
        lines.append("")
    lines.append(f"Held: CONTINUE {res['continue']}, MONITOR {res['monitor']}")
    lines.append(ads_link())
    return "\n".join(lines)


def msg_weekly(res, cstar, start, end):
    isos = res['isolates']
    integ = integrity_line(res)
    if not isos and not res['geo_budget'] and not res['geo_conv']:
        return f":memo: BFC-VOLUME weekly review ({start} to {end}): no isolate/geo actions.\n{integ}"
    lines = [f":memo: *BFC-VOLUME weekly review* ({start} to {end}, DEL BOOKNOW, 7-day)",
             "_Scale/isolate + structural geo layer. Daily handles kills/brake/prune._", integ, ""]
    if isos:
        lines.append(f"*ISOLATE candidates* (<=0.7x median, >=12 BC -> own ad set) ({len(isos)})")
        for (c, lyr, need, lb, sp, x) in isos: lines.append(_row(c, lyr, need, lb, sp, x, "break into own ad set"))
        lines.append("")
    if res['geo_budget']:
        lines.append(f"*Geo budget* (7d CPBC vs C* ~Rs{cstar:,.0f})" if cstar else "*Geo budget*")
        for (g, a, cp, bf) in sorted(res['geo_budget']): lines.append(f"   - *{g}*: {a} - CPBC Rs{cp:,.0f}, {int(bf)} BC")
        lines.append("")
    if res['geo_conv']:
        lines.append("*Geo diagnostic* (7d, vs Delhi benchmark)")
        for (g, flags, _s) in res['geo_conv']:
            lines.append(f"   *{g}*:")
            for f in flags: lines.append(f"      - {f}")
        lines.append("")
    lines.append(ads_link())
    return "\n".join(lines)


def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def slack_post(text, dm_only=False):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token: raise SystemExit("SLACK_BOT_TOKEN not set (use --dry-run to print instead).")
    targets = []
    dm = os.environ.get('SLACK_DM_USER_ID', SLACK_DM_DEFAULT)
    op = slack_api('conversations.open', token, {'users': dm})
    if op.get('ok'): targets.append(('DM', op['channel']['id']))
    else: print("warn: conversations.open failed:", op.get('error'))
    if not dm_only:
        targets.append(('#growth-reports', os.environ.get('SLACK_CHANNEL_ID', SLACK_CHANNEL_DEFAULT)))
    for label, ch in targets:
        resp = slack_api('chat.postMessage', token, {'channel': ch, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
        print(f"posted to {label} ({ch}):", 'ok' if resp.get('ok') else resp.get('error'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['daily', 'weekly'], default='daily')
    ap.add_argument('--dry-run', action='store_true', help='print the message, do not post or write log')
    ap.add_argument('--dm-only', action='store_true', help='post to the DM copy only (testing), skip the channel')
    ap.add_argument('--date', help='override D-1 anchor YYYY-MM-DD (default = yesterday IST)')
    ap.add_argument('--no-post', action='store_true',
                     help='run for real (write log/state) but skip Slack - for backfilling logs on corrected '
                          'data without re-notifying; print the message instead of posting it')
    ap.add_argument('--last-retry', action='store_true',
                     help='final scheduled attempt of the day - alert if dashboard data is still not ready, '
                          'instead of quietly postponing to the next retry')
    args = ap.parse_args()
    load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    # Dashboard-readiness gate (v2.4.0, 2026-07-17): only for real, unattended
    # scheduled runs - a --dry-run preview or a --date backtest is an
    # intentional manual action, not subject to the retry schedule. Skipped
    # for --mode weekly too (not part of this request; the weekly review's
    # own schedule is unchanged). See dashboard_readiness.py for why (D-1
    # data has been landing at erratic times, once crashed a run outright)
    # and daily-rule-pass.yml for the 13:30/15:30/17:30 IST retry triggers.
    if args.mode == 'daily' and not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today, mark_completed_today
        if already_completed_today('rule_pass', d1):
            print(f'already completed for {d1} - skipping (idempotent retry guard)')
            return
        ready, dash_total, actual_total = is_dashboard_data_ready(d1)
        if not ready:
            dash_s = f"Rs{dash_total:,.0f}" if dash_total is not None else 'n/a'
            act_s = f"Rs{actual_total:,.0f}" if actual_total is not None else 'n/a'
            if args.last_retry:
                token = os.environ.get('SLACK_BOT_TOKEN')
                if token:
                    slack_post(
                        f":rotating_light: *BFC-VOLUME daily kill+prune* - dashboard data for {d1} still "
                        f"incomplete after 3 attempts (dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). "
                        f"Pass did NOT run today - check the dashboard ETL.",
                        dm_only=False)
                print(f'last retry - data still not ready for {d1} (dashboard={dash_s}, actual={act_s}) - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat(); end = d1.isoformat()
    active, ad_ids_map = meta_active_del()
    last_activation = get_last_activation_dates(d1, active)
    data, age, cstar, funnel_geo = compute(d1, last_activation)
    res = decide(data, age, cstar, active, funnel_geo=funnel_geo)

    # Live same-day spend cross-check (2026-08-17, Nikhil) - see meta_today_spend()
    # docstring. Same treatment as trailing-7d top-spender: pull the concept out of KILL
    # entirely (verdict -> MONITOR) into deferred_live_spend, rendered under PROTECTED
    # (tagged separately so the reason is clear) - not just a warning label on a KILL
    # line, since a KILL still reads as "recommended" even with a warning attached. Only
    # meaningful for a live daily run against real D-1 data; skip for --date backtests.
    res['deferred_live_spend'] = []
    if args.mode == 'daily' and not args.date:
        today_ist = d1 + datetime.timedelta(days=1)
        today_spend = meta_today_spend(ad_ids_map, today_ist)
        if today_spend:
            top_today = {c for c in sorted(today_spend, key=lambda c: -today_spend[c])[:2] if today_spend[c] > 0}
            if top_today:
                still_kills = []
                for k in res['kills']:
                    if k[0] in top_today:
                        res['deferred_live_spend'].append(k)
                        res['verdict'][k[0]] = 'MONITOR'
                        res['monitor'] += 1
                    else:
                        still_kills.append(k)
                res['kills'] = still_kills

    # Logging (skip in dry-run)
    # 2026-08-18: broadened from just top-spender/live-spend to "still over-threshold
    # today in any form" - see retro_check() docstring (JUN26-T-064 case: median drifted
    # up past a creative's own unchanged CPBC, so it's no longer a kill candidate today
    # even though it isn't top-spender-protected or a live-spend-surge case).
    still_flagged_today = ({k[0] for k in res.get('kills', [])} |
                            {k[0] for k in res.get('deferred_kills', [])} |
                            {t[0] for t in res.get('deferred_top_spender', [])} |
                            {t[0] for t in res.get('deferred_live_spend', [])})
    unacted = []
    if args.mode == 'daily' and not args.dry_run:
        log = load_log()
        unacted = retro_check(log, d1, still_flagged_today=still_flagged_today)
        write_action_log_csv(log, d1)
        log = write_log_entry(log, res, d1, ad_ids_map, res['median'])
        save_log(log)
        sheet_sync(log, d1)
        git_commit_log(d1)
    elif args.mode == 'daily' and args.dry_run:
        # Show what retro check would say, without writing anything
        log = load_log()
        unacted = retro_check(load_log(), d1, still_flagged_today=still_flagged_today)  # read-only check for display

    msg = msg_daily(res, cstar, end, unacted=unacted) if args.mode == 'daily' else msg_weekly(res, cstar, start, end)
    if args.dry_run or args.no_post: print(msg)
    else: slack_post(msg, dm_only=args.dm_only)

    if args.mode == 'daily' and not args.dry_run and not args.date:
        from dashboard_readiness import mark_completed_today
        mark_completed_today('rule_pass', d1)  # after the post succeeds, so a crash mid-run allows retry


if __name__ == '__main__':
    main()
