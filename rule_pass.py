# -*- coding: utf-8 -*-
"""BFC-VOLUME kill + prune pass -> Slack. Operating Spec v2.2.0 (post SD sign-off, 1 Jul 2026).

  DAILY (post-ETL): efficiency kill + zero-BFC kill + cost-velocity brake (KILL-REVIEW) + pool-cap
        prune cut-list. Lifetime CPBFC (booking_confirmed), lifetime 5-BFC gate, NO calendar grace,
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
CREATIVE_BFC_GATE = 5             # LIFETIME bfc to be efficiency-killable
AGE_GRACE_DAYS    = 7             # v2.3.0: below CREATIVE_BFC_GATE is fine within the first 7d
                                    # of deployment; past that, still-thin + bad CPBFC + under the
                                    # brake spend floor becomes kill-eligible (closes the case where
                                    # a creative never crosses either gate and sits in MONITOR forever)
ZERO_BFC_SPEND    = 10000         # Rs lifetime spend, 0 bfc -> kill
KILL_MULT         = {'L1': 1.0, 'L2': 1.0, 'L3': 1.2, 'untagged': 1.0}
DAILY_KILL_CAP    = 3             # v2.2.0: if efficiency-kill candidates > 3, rank by ratio worst-first, cap at 3
TOP_SPENDER_SHARE = 0.10          # v2.2.0: warn (not block) if kill candidate holds >10% of pool daily avg spend
L3_FLIP           = False         # Discovery box not operational -> L3 holds 1.2x (auto-flip to 1.0 later)
BLENDED_TARGET    = 500           # Rs; C* = BLENDED_TARGET * totalBFC / paidBFC
BRAKE_SPEND_FLOOR = 15000         # Rs
BRAKE_CSTAR_MULT  = 5
BRAKE_CPBFC_MULT  = 2.0           # x the creative kill line
ISOLATE_MULT      = 0.7
ISOLATE_BFC_GATE  = 12
POOL_CAP          = 15
MATURE_GEOS       = {'Delhi'}
GEO_BUDGET_BFC_GATE = 10
GEO_CONV_INSTALLS = 100
GEO_CONV_MULT     = 2.0
GEO_RUNAWAY_SPEND = 50000
DASH_BASE         = 'https://growth-portal.up.railway.app'
ADS_MANAGER       = 'https://adsmanager.facebook.com/adsmanager/manage/ads'
CONCEPT_RE        = re.compile(r'JUN26-[TCRH]-\d{3}')
SLACK_CHANNEL_DEFAULT = 'C0B9G0Q68G6'   # #growth-reports
SLACK_DM_DEFAULT      = 'U05A9037VFG'   # Nikhil
META_ACC_DEFAULT      = '2007675312900454'
META_VER_DEFAULT      = 'v23.0'

# ---- logging constants ----
LOG_PATH        = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kill_pass_log.json')
ACTION_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'kill_action_log.csv')
SHEET_ID        = '145hcZtsX_W-ibI5SrW9tksVO0J-Tka9NtuIaIpZglqA'
SHEET_TAB       = 'Recos'


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


def retro_check(log, d1):
    """Back-fill action_taken for yesterday's KILL entries. Returns list of unacted recos."""
    yesterday = (d1 - datetime.timedelta(days=1)).isoformat()
    tok = os.environ.get('META_ACCESS_TOKEN')
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
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
            if reco.get('action_taken') == 'No':
                unacted.append(reco)
    return unacted


def write_log_entry(log, res, d1, ad_ids_map, median):
    """Append today's KILL recos to the log (idempotent - replaces any existing entry for d1)."""
    recos = []
    for (c, lyr, need, lb, sp, x, reason) in res['kills']:
        recos.append({
            'concept_id': c,
            'layer': lyr,
            'need': need,
            'verdict': 'KILL',
            'reason': reason,
            'cpbfc': round(x, 2) if x != float('inf') else None,
            'bfc': int(lb),
            'spend': round(sp, 2),
            'median_at_time': round(median, 2) if median else None,
            'ad_ids': ad_ids_map.get(c, []),
            'action_taken': None,
            'action_timing': None,
        })
    log = [e for e in log if e['date'] != d1.isoformat()]
    log.append({
        'date': d1.isoformat(),
        'median_cpbfc': round(median, 2) if median else None,
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
                x = reco.get('cpbfc')
                med = reco.get('median_at_time')
                ws.append_row([
                    entry['date'],
                    reco['concept_id'],
                    reco['verdict'],
                    reco.get('reason', ''),
                    f"{x:,.0f}" if x is not None else 'inf',
                    reco.get('bfc', ''),
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
                reco.get('bfc', ''),
                reco.get('spend', ''),
                reco.get('cpbfc', ''),
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
                w.writerow(['reco_date', 'concept_id', 'layer', 'need_state', 'bfc', 'spend',
                            'cpbfc', 'median_at_time', 'reason', 'ad_ids',
                            'action_taken', 'action_timing'])
            w.writerows(rows)
        print(f'action log: wrote {len(rows)} row(s) for {yesterday}')
    except Exception as e:
        print(f'warn: could not write action log - {e}')


def git_commit_log(d1):
    """Commit the updated log JSON back to the repo."""
    try:
        repo = os.path.dirname(os.path.abspath(__file__))
        subprocess.run(['git', 'add', LOG_PATH, ACTION_LOG_PATH], cwd=repo, check=True, capture_output=True)
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


def compute(d1):
    metric_start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = dget('/api/master_export?' + urllib.parse.urlencode({'start': CAMPAIGN_START, 'end': d1.isoformat()}))
    data = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'spend': 0.0, 'bfc': 0, 'inst': 0, 'w7s': 0.0, 'w7i': 0, 'layer': 'untagged', 'need': '?'}))
    first = {}
    for r in rows:
        if r.get('channel') != 'META' or 'BFC-VOLUME' not in str(r.get('campaign', '')).upper(): continue
        nm = str(r.get('creative', ''))
        if 'BOOKNOW' not in nm.upper(): continue
        m = CONCEPT_RE.search(nm)
        if not m: continue
        cid = m.group(0); g = geo_of(r.get('ad_set', '')) or geo_of(r.get('campaign', '')) or 'Other'
        dt = str(r.get('date', '')); sp = r.get('spend') or 0; bf = r.get('booking_confirmed') or 0; ins = r.get('app_installs') or 0
        rec = data[g][cid]
        rec['spend'] += sp; rec['bfc'] += bf; rec['inst'] += ins
        rec['layer'] = layer_of(nm); rec['need'] = need_of(nm)
        if sp > 0 and (cid not in first or dt < first[cid]): first[cid] = dt
        if dt >= metric_start:
            rec['w7s'] += sp; rec['w7i'] += ins; rec['w7b'] = rec.get('w7b', 0) + bf
    # ---- funnel rows for non-Delhi geo diagnostic (7-day window) ----
    funnel_geo = collections.defaultdict(lambda: {'inst': 0, 'svc_check': 0, 'svc_true': 0, 'bfc': 0, 'w7s': 0.0})
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
            fg['bfc']       += r.get('booking_confirmed') or 0
        # pull 7-day spend per non-Delhi geo from master_export (already iterated above via data)
        for g, wc in data.items():
            if g == 'Other': continue
            funnel_geo[g]['w7s'] = sum(x['w7s'] for x in wc.values())
    except Exception as e:
        print(f'warn: funnel_rows fetch failed - {e}')
    age = {}
    for cid, ds in first.items():
        try: age[cid] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception: age[cid] = 999
    cstar = None
    try:
        wr = dget('/api/war_room?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        days = wr.get('days', wr) if isinstance(wr, dict) else wr
        tot = sum(d.get('bookings') or 0 for d in days)
        paid = sum((d.get('meta_bfc') or 0) + (d.get('google_bfc') or 0) for d in days)
        if paid: cstar = BLENDED_TARGET * tot / paid
    except Exception: pass
    return data, age, cstar, dict(funnel_geo)


def cpbfc(rec): return rec['spend'] / rec['bfc'] if rec['bfc'] else float('inf')


def decide(data, age, cstar, active, funnel_geo=None):
    res = {'kills': [], 'reviews': [], 'isolates': [], 'prune_cut': [], 'pool_n': 0, 'continue': 0,
           'monitor': 0, 'median': None, 'brake_spend': None, 'geo_budget': [], 'geo_conv': [],
           'active_filter': active is not None}
    pool = data.get('Delhi', {})
    spent = [c for c in pool if pool[c]['spend'] > 0]
    def act(c): return (active is None) or (c in active)
    elig_active = [c for c in spent if act(c) and pool[c]['bfc'] >= CREATIVE_BFC_GATE]
    med = statistics.median([cpbfc(pool[c]) for c in elig_active]) if elig_active else None
    res['median'] = med
    brake_spend = max(BRAKE_CSTAR_MULT * cstar, BRAKE_SPEND_FLOOR) if cstar else BRAKE_SPEND_FLOOR
    res['brake_spend'] = brake_spend
    verdict = {}
    eff_kill_candidates = []  # (c, lyr, need, lb, sp, x, reason, ratio) - capped below
    for c in spent:
        if not act(c): continue
        rec = pool[c]; lb = rec['bfc']; sp = rec['spend']; x = cpbfc(rec); lyr = rec['layer']
        mult = KILL_MULT.get(lyr, 1.0)
        if lyr == 'L3' and L3_FLIP: mult = 1.0
        kt = (mult * med) if med else None
        if lb == 0 and sp >= ZERO_BFC_SPEND:
            verdict[c] = 'KILL'; res['kills'].append((c, lyr, rec['need'], lb, sp, x, 'zero-BFC')); continue
        if kt and sp >= brake_spend and x >= BRAKE_CPBFC_MULT * kt:
            verdict[c] = 'KILL_REVIEW'; res['reviews'].append((c, lyr, rec['need'], lb, sp, x, f'brake (>=2x line, spend Rs{sp:,.0f})')); continue
        if lb >= CREATIVE_BFC_GATE and kt:
            if x >= kt:
                eff_kill_candidates.append((c, lyr, rec['need'], lb, sp, x, f'efficiency (>= {mult}x median Rs{med:,.0f})', x / med))
                continue
            if x <= ISOLATE_MULT * med and lb >= ISOLATE_BFC_GATE:
                verdict[c] = 'ISOLATE'; res['isolates'].append((c, lyr, rec['need'], lb, sp, x)); continue
            verdict[c] = 'CONTINUE' if x < med else 'MONITOR'
        elif kt and age.get(c, 0) > AGE_GRACE_DAYS and x >= kt:
            # v2.3.0: aged-out - past the 7-day grace window, still below the lifetime
            # BFC gate (thin sample) and under the brake spend floor (else the brake
            # check above would already have caught it), but already CPBFC-bad enough
            # to fail efficiency if it had reached the gate.
            eff_kill_candidates.append((c, lyr, rec['need'], lb, sp, x,
                f'aged-out (>{AGE_GRACE_DAYS}d, {lb} BFC, >= {mult}x median Rs{med:,.0f})', x / med))
        else:
            verdict[c] = 'MONITOR'
    # v2.2.0: daily kill cap - rank by ratio (worst first), kill top DAILY_KILL_CAP, defer rest to MONITOR
    eff_kill_candidates.sort(key=lambda t: -t[7])
    res['deferred_kills'] = []
    for i, (c, lyr, need, lb, sp, x, reason, _ratio) in enumerate(eff_kill_candidates):
        if i < DAILY_KILL_CAP:
            verdict[c] = 'KILL'
            res['kills'].append((c, lyr, need, lb, sp, x, reason))
        else:
            verdict[c] = 'MONITOR'
            res['deferred_kills'].append((c, lyr, need, lb, sp, x, reason))
    # v2.2.0: top-spender warning - flag if KILL/KILL-REVIEW candidate is #1 or #2 by 7-day spend AND >10% pool share
    pool_w7s = {c: pool[c]['w7s'] for c in spent if act(c)}
    pool_total_w7s = sum(pool_w7s.values())
    top2 = sorted(pool_w7s, key=lambda c: -pool_w7s[c])[:2]
    kill_ids = {k[0] for k in res['kills']} | {r[0] for r in res['reviews']}
    res['top_spender_warns'] = {
        c for c in top2
        if c in kill_ids and pool_total_w7s > 0 and pool_w7s[c] / pool_total_w7s > TOP_SPENDER_SHARE
    }
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
            best = sorted(mem, key=lambda c: (-pool[c]['w7s'], cpbfc(pool[c])))[0]; keep.add(best)
        rest = [c for c in monitor if c not in keep]
        ws = [pool[c]['w7s'] for c in rest]
        ineff = [(cpbfc(pool[c]) / med if (pool[c]['bfc'] and med) else 1.0) for c in rest]
        def z(v, arr):
            mu = sum(arr) / len(arr) if arr else 0
            sd = (statistics.pstdev(arr) if len(arr) > 1 else 1) or 1
            return (v - mu) / sd
        score = {c: z(pool[c]['w7s'], ws) - z((cpbfc(pool[c]) / med if (pool[c]['bfc'] and med) else 1.0), ineff) for c in rest}
        for c in sorted(rest, key=lambda c: -score[c]):
            if len(keep) < POOL_CAP: keep.add(c)
        res['prune_cut'] = sorted([c for c in survivors if c not in keep], key=lambda c: pool[c]['w7s'])
    # ---- weekly: geo budget (mature geo, 7-day cpbfc vs C*) ----
    if cstar:
        for g in MATURE_GEOS:
            wc = data.get(g, {})
            gsp7 = sum(x['w7s'] for x in wc.values()); gbfc7 = sum(x.get('w7b', 0) for x in wc.values())
            if gbfc7 >= GEO_BUDGET_BFC_GATE:
                gcp = gsp7 / gbfc7
                res['geo_budget'].append((g, 'SCALE' if gcp <= cstar else 'HOLD', gcp, gbfc7))
    # ---- weekly: non-Delhi geo 3-stage diagnostic (7-day spend gate) ----
    fg = funnel_geo or {}
    del_fg = fg.get('Delhi', {}); del_w7s = sum(x['w7s'] for x in data.get('Delhi', {}).values())
    del_svc_check = del_fg.get('svc_check', 0); del_svc_true = del_fg.get('svc_true', 0); del_bfc = del_fg.get('bfc', 0)
    del_cpsc  = (del_w7s / del_svc_check) if del_svc_check else None
    del_svc_rate  = (del_svc_true / del_svc_check) if del_svc_check else None
    del_conv_rate = (del_bfc / del_svc_true) if del_svc_true else None
    for g, gf in fg.items():
        if g in MATURE_GEOS: continue
        w7s = gf.get('w7s', 0)
        if w7s <= 0: continue   # not active in last 7 days
        svc_check = gf.get('svc_check', 0); svc_true = gf.get('svc_true', 0); bfc = gf.get('bfc', 0)
        if svc_check < GEO_CONV_INSTALLS: continue
        g_cpsc       = w7s / svc_check if svc_check else None
        g_svc_rate   = svc_true / svc_check if svc_check else None
        g_conv_rate  = bfc / svc_true if svc_true else None
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
    base = f"   - `{c}` [{lyr}/{need}] {int(lb)} BFC, Rs{sp:,.0f}, CPBFC Rs{xs}"
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
    if not kills and not reviews and not cut and not unacted and not res.get('deferred_kills'):
        return f":white_check_mark: *BFC-VOLUME daily kill+prune* ({end}, DEL BOOKNOW, lifetime): no kills, no brake, no prune. Pool {res['pool_n']}/{POOL_CAP}.\n{integ}"
    medlabel = "active-only median" if res['active_filter'] else "median (incl. paused)"
    if res['median']:
        head = (f":scales: *BFC-VOLUME daily kill + prune* ({end}, DEL BOOKNOW, lifetime)\n"
                f"{medlabel} CPBFC Rs{res['median']:,.0f} | C* Rs{cstar:,.0f} | brake Rs{res['brake_spend']:,.0f} | pool {res['pool_n']}/{POOL_CAP}\n"
                f"_Decisions for review - read-only, pausing is a manual step in Ads Manager._")
    else:
        head = f":scales: *BFC-VOLUME daily kill + prune* ({end})"
    lines = [head, integ, ""]
    if unacted:
        lines.append(f":warning: *NOT ACTED UPON - yesterday's KILLs still ACTIVE ({len(unacted)})*")
        for reco in unacted:
            x = reco.get('cpbfc')
            xs = f"{x:,.0f}" if x is not None else 'inf'
            lines.append(f"   - `{reco['concept_id']}` {reco.get('bfc', '?')} BFC, CPBFC Rs{xs} - still running, pause in Ads Manager")
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
        lines.append(f"*ISOLATE candidates* (<=0.7x median, >=12 BFC -> own ad set) ({len(isos)})")
        for (c, lyr, need, lb, sp, x) in isos: lines.append(_row(c, lyr, need, lb, sp, x, "break into own ad set"))
        lines.append("")
    if res['geo_budget']:
        lines.append(f"*Geo budget* (7d CPBFC vs C* ~Rs{cstar:,.0f})" if cstar else "*Geo budget*")
        for (g, a, cp, bf) in sorted(res['geo_budget']): lines.append(f"   - *{g}*: {a} - CPBFC Rs{cp:,.0f}, {int(bf)} BFC")
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
    args = ap.parse_args()
    load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()
    start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat(); end = d1.isoformat()
    data, age, cstar, funnel_geo = compute(d1)
    active, ad_ids_map = meta_active_del()
    res = decide(data, age, cstar, active, funnel_geo=funnel_geo)

    # Logging (skip in dry-run)
    unacted = []
    if args.mode == 'daily' and not args.dry_run:
        log = load_log()
        unacted = retro_check(log, d1)
        write_action_log_csv(log, d1)
        log = write_log_entry(log, res, d1, ad_ids_map, res['median'])
        save_log(log)
        sheet_sync(log, d1)
        git_commit_log(d1)
    elif args.mode == 'daily' and args.dry_run:
        # Show what retro check would say, without writing anything
        log = load_log()
        unacted = retro_check(load_log(), d1)  # read-only check for display

    msg = msg_daily(res, cstar, end, unacted=unacted) if args.mode == 'daily' else msg_weekly(res, cstar, start, end)
    if args.dry_run: print(msg)
    else: slack_post(msg, dm_only=args.dm_only)


if __name__ == '__main__':
    main()
