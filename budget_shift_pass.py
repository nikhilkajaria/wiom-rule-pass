# -*- coding: utf-8 -*-
"""Budget channel rebalancing pass -> Slack. Spec v2.1.

  Runs daily after kill pass (07:30 IST). Checks 7-day rolling Branch-attributed
  CPBL per channel (Meta vs Google). Trigger: gap > 10% for 3 consecutive days.

  Tiered stabilization (v2.0, per Guneet/Nikhil alignment 2026-07-11):
    - gap <= 15% ("FAST_ITERATE_GAP"): safe to settle - full 7-day stabilization,
      no new shifts until it completes.
    - gap  > 15% for 3 consecutive days: don't wait - keep iterating every
      3 days. This ALSO breaks an active stabilization early if the gap
      re-widens past 15% for 3 days while parked (the exact gap that let the
      Jul 8-10 gap run unaddressed through a stale stabilization window).

  Sizing (v2.0): each step's total move is a channel-level ceiling
  (min(gap/2, 15%) x Meta total, capped at 15% x Google total). That total is
  then DISTRIBUTED - not dumped into one ad set/campaign:
    - Meta: drawn from ALL in-scope ad sets worst-CPBL-first, each capped at
      15% of its own budget - so the shift stops loading entirely onto
      whichever ad set happens to be biggest, and instead hits the worst
      performers first. RETARGETING became eligible 2026-07-13 (v2.1) - it
      was excluded by default before, but two independent reads (point-in-
      time CPBL and the 2-week trend) agreed it had become the weakest Meta
      performer, not just a thin-sample blip, so the blanket exclusion no
      longer held.
    - Google: funded into in-scope campaigns best-CPBL-first, each ALSO capped
      at 15% of its own budget (previously the whole channel-level amount
      could land on one campaign, silently exceeding that campaign's own 15%
      anti-shock cap - the same discipline now applies to both sides).
  Ad sets whose CPBL is >= 2x the best Meta performer are flagged as pause
  candidates in the message (advisory only - pausing is always a human call,
  never sized into the automatic allocation).

  Posts recommendation to Slack; human approves and executes. Read-only -
  it NEVER writes to any ad platform.

Run:  python budget_shift_pass.py  [--dry-run] [--dm-only] [--date YYYY-MM-DD]
      python budget_shift_pass.py --reset          # clear shift state
Env (Actions secrets / local C:\\credentials\\.env):
      WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN, SLACK_BOT_TOKEN,
      GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
      GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID
"""
import sys, io, os, json, csv, re, argparse, datetime, collections, urllib.request, urllib.parse
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- spec constants (v2.0) ----
TRIGGER_GAP          = 0.10   # channel CPBL gap threshold to consider any shift
FAST_ITERATE_GAP     = 0.15   # above this, don't wait out stabilization - keep iterating
TRIGGER_DAYS         = 3      # consecutive days above threshold to fire/override
MAX_STEP_PCT         = 0.15   # max budget change per step, PER ad set/campaign (both sides)
STEP_CADENCE_DAYS    = 1      # days between steps (was 3 - too slow when the gap is this
                               # large; Nikhil, 2026-07-13. Note this compounds fast at
                               # MAX_STEP_PCT=15%/step - see red flags in the same commit)
STABILIZATION_DAYS   = 7      # days of read after gap cools to <= FAST_ITERATE_GAP
MIN_BC_FOR_CPBL     = 20     # min 7-day BC to trust an ad set's CPBL without caveat
PAUSE_CANDIDATE_MULT = 2.0    # flag a Meta source as a pause candidate at >= this x the best CPBL
MONITORING_THRESH    = 0.20   # flag if metric moves >20% on both DoD and WoW
DASH_BASE            = 'https://growth-portal.up.railway.app'
META_ACC_DEFAULT     = '2007675312900454'
META_VER_DEFAULT     = 'v23.0'
GOOGLE_CID_DEFAULT   = '1218037894'
SLACK_CHANNEL_DEFAULT = 'C0B9G0Q68G6'  # #growth-reports
SLACK_DM_DEFAULT      = 'U05A9037VFG'  # Nikhil

# in-scope campaign name substrings (case-insensitive)
META_IN_SCOPE  = ['BFC-VOLUME', 'RETARGETING']
GOOGLE_IN_SCOPE = ['UAC', 'DEMANDGEN', 'SEARCH']
GOOGLE_TOF_EXCLUDE = ['AWARENESS']  # exclude ToF from budget pool

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH      = os.path.join(_DIR, 'budget_shift_state.json')
ACTION_LOG_PATH = os.path.join(_DIR, 'budget_shift_log.csv')


# ---- env ----

def load_env():
    path = r'C:\credentials\.env'
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1)
                k = k.strip()
                if k not in os.environ:
                    os.environ[k] = v.strip().strip('"').strip("'")


# ---- state ----

def load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            print(f'warn: could not read state - {e}')
    return {'phase': 'none', 'shift': None, 'stabilization_end': None}


def save_state(state):
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ---- data fetching ----

def dget(path):
    req = urllib.request.Request(
        DASH_BASE + path,
        headers={'X-Dashboard-Token': os.environ['WIOM_DASHBOARD_TOKEN'], 'User-Agent': 'wiom-budget-shift'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def get_war_room(d1):
    """Last 14 days for trigger check + monitoring DoD/WoW."""
    start = (d1 - datetime.timedelta(days=13)).isoformat()
    data = dget('/api/war_room?' + urllib.parse.urlencode({'start': start, 'end': d1.isoformat()}))
    days = data.get('days', data) if isinstance(data, dict) else data
    return {d['date']: d for d in days}


def get_efficiency_data(d1, lookback=6):
    """
    7-day spend + booking_confirmed per Meta ad_set and Google campaign, via the
    raw /api/raw/days attribution[] + meta_spend[] endpoints - NOT master_export,
    whose Google-side booking_confirmed field reads 0 for every row (confirmed
    2026-07-11). Google is aggregated at the CAMPAIGN level throughout: Search's
    attribution rows only populate `campaign` (ad_set is blank there), while
    UAC/DemandGen already collapse ad_set==campaign - campaign is the one join
    key that works for every Google campaign type.
    Returns (meta_by_adset, google_by_campaign), each {name: {'spend':, 'bc':}}.
    """
    start = (d1 - datetime.timedelta(days=lookback)).isoformat()
    data = dget('/api/raw/days?' + urllib.parse.urlencode({'start': start, 'end': d1.isoformat()}))
    records = data.get('records', data) if isinstance(data, dict) else data

    meta = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0})
    goog = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0})
    for day in records:
        for r in day.get('meta_spend', []):
            if r.get('channel') == 'META':
                meta[r.get('adset_name')]['spend'] += float(r.get('spend') or 0)
            elif r.get('channel') == 'GOOGLE':
                goog[r.get('campaign_name')]['spend'] += float(r.get('spend') or 0)
        for r in day.get('attribution', []):
            camp = r.get('campaign') or ''
            bc = int(r.get('booking_confirmed') or 0)
            if 'GOOGLE' in camp.upper():
                goog[camp]['bc'] += bc
            else:
                meta[r.get('ad_set')]['bc'] += bc
    return dict(meta), dict(goog)


def get_meta_budgets():
    """Active ad set daily budgets for in-scope Meta campaigns."""
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok: return {}
    acc = 'act_' + os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT).replace('act_', '')
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)

    # Get active in-scope campaign IDs
    url = (f'https://graph.facebook.com/{ver}/{acc}/campaigns?'
           f'fields=id,name,effective_status&limit=100&access_token={tok}')
    with urllib.request.urlopen(url, timeout=30) as r:
        camps = json.loads(r.read().decode()).get('data', [])

    budgets = {}  # adset_name -> {id, daily_budget, campaign_type}
    for c in camps:
        if c.get('effective_status') != 'ACTIVE': continue
        cname = c.get('name', '').upper()
        ctype = next((t for t in META_IN_SCOPE if t in cname), None)
        if not ctype: continue
        url2 = (f'https://graph.facebook.com/{ver}/{c["id"]}/adsets?'
                f'fields=id,name,effective_status,daily_budget&limit=100&access_token={tok}')
        with urllib.request.urlopen(url2, timeout=30) as r:
            adsets = json.loads(r.read().decode()).get('data', [])
        for a in adsets:
            if a.get('effective_status') != 'ACTIVE': continue
            db = int(a.get('daily_budget') or 0) / 100
            if db > 0:
                budgets[a['name']] = {'id': a['id'], 'daily_budget': db, 'type': ctype}
    return budgets


def get_google_budgets():
    """Active campaign daily budgets for in-scope Google campaigns."""
    try:
        from google.ads.googleads.client import GoogleAdsClient
        config = {
            'developer_token': os.environ['GOOGLE_ADS_DEVELOPER_TOKEN'],
            'client_id': os.environ['GOOGLE_ADS_CLIENT_ID'],
            'client_secret': os.environ['GOOGLE_ADS_CLIENT_SECRET'],
            'refresh_token': os.environ['GOOGLE_ADS_REFRESH_TOKEN'],
            'login_customer_id': os.environ.get('GOOGLE_ADS_CUSTOMER_ID', GOOGLE_CID_DEFAULT).replace('-', ''),
            'use_proto_plus': True,
        }
        client = GoogleAdsClient.load_from_dict(config)
        ga = client.get_service('GoogleAdsService')
        cid = config['login_customer_id']
        query = '''SELECT campaign.id, campaign.name, campaign_budget.amount_micros
                   FROM campaign WHERE campaign.status = ENABLED
                   ORDER BY campaign_budget.amount_micros DESC'''
        budgets = {}
        for row in ga.search(customer_id=cid, query=query):
            name  = row.campaign.name
            name_up = name.upper()
            if any(ex in name_up for ex in GOOGLE_TOF_EXCLUDE): continue
            ctype = next((t for t in GOOGLE_IN_SCOPE if t in name_up), None)
            if not ctype: continue
            daily_rs = row.campaign_budget.amount_micros / 1_000_000
            budgets[name] = {'id': str(row.campaign.id), 'daily_budget': daily_rs, 'type': ctype}
        return budgets
    except Exception as e:
        print(f'warn: Google Ads budgets unavailable - {e}')
        return {}


# ---- trigger logic ----

def _consecutive_days_over(war_room, d1, threshold):
    """Walk back 14 days from d1, count consecutive days where
    gap=(meta_cpbl-google_cpbl)/meta_cpbl > threshold. Returns
    (consecutive, gap_today, meta_cpbl_today, google_cpbl_today)."""
    consecutive = 0
    gap_today = None
    meta_cpbl_today = None
    google_cpbl_today = None
    for i in range(13, -1, -1):  # walk backwards from d1
        date_str = (d1 - datetime.timedelta(days=i)).isoformat()
        day = war_room.get(date_str, {})
        mc = day.get('meta_cpbl')
        gc = day.get('google_cpbl')
        if mc and gc and mc > 0:
            gap = (mc - gc) / mc
            if gap > threshold:
                consecutive += 1
            else:
                consecutive = 0
            if i == 0:
                gap_today = gap
                meta_cpbl_today = mc
                google_cpbl_today = gc
    return consecutive, gap_today, meta_cpbl_today, google_cpbl_today


def check_trigger(war_room, d1):
    """Base trigger: gap > TRIGGER_GAP(10%) for TRIGGER_DAYS(3) consecutive days.
    Returns (fires, gap_today, consecutive_days, meta_cpbl, google_cpbl)."""
    consecutive, gap_today, meta_cpbl, google_cpbl = _consecutive_days_over(war_room, d1, TRIGGER_GAP)
    fires = consecutive >= TRIGGER_DAYS
    return fires, gap_today, consecutive, meta_cpbl, google_cpbl


def check_fast_iterate(war_room, d1):
    """Override: gap > FAST_ITERATE_GAP(15%) for TRIGGER_DAYS(3) consecutive days ->
    don't settle into (or stay in) stabilization; keep iterating every
    STEP_CADENCE_DAYS instead. Returns (fires, gap_today, consecutive_days)."""
    consecutive, gap_today, _, _ = _consecutive_days_over(war_room, d1, FAST_ITERATE_GAP)
    return consecutive >= TRIGGER_DAYS, gap_today, consecutive


def compute_stab_end(last_step_date_str, last_step_time_str):
    """
    7-day stabilization window anchored to the LAST REAL shift (not to whatever
    day a later step-boundary check happens to notice the gap already closed).

    Day-counting: the action's own timestamp decides whether its calendar day
    counts as day 1 of the window (D1) or is excluded (D0):
      - action at/after 12:00 IST  -> that day is D0 (not counted); window is
        D0+1 .. D0+7, i.e. stab_end = last_step_date + 7
      - action before 12:00 IST    -> that day is D1 (counted); window is
        D1 .. D1+6, i.e. stab_end = last_step_date + 6
    Unknown/missing time defaults to the pre-noon (D1) case - the more
    conservative option, since it ends stabilization a day sooner rather than
    silently extending it.
    """
    last_date = datetime.date.fromisoformat(last_step_date_str)
    post_noon = False
    if last_step_time_str:
        try:
            post_noon = datetime.datetime.fromisoformat(last_step_time_str).hour >= 12
        except Exception:
            post_noon = False
    offset = STABILIZATION_DAYS if post_noon else STABILIZATION_DAYS - 1
    return (last_date + datetime.timedelta(days=offset)).isoformat()


# ---- sizing + distribution (v2.0) ----

def compute_shift_target(gap, meta_total_budget, google_total_budget):
    """Channel-level ceiling for this step, BEFORE distributing across multiple
    source/destination ad sets/campaigns (each capped at their own 15% inside
    allocate())."""
    step_pct = min(gap / 2, MAX_STEP_PCT)
    term_meta_ch   = step_pct * meta_total_budget
    term_google_ch = MAX_STEP_PCT * google_total_budget
    return min(term_meta_ch, term_google_ch)


def rank_meta_sources(meta_budgets, meta_eff):
    """All in-scope Meta ad sets (BFC-VOLUME AND RETARGETING) ranked worst-CPBL-first.
    RETARGETING became eligible 2026-07-13 - two independent reads (point-in-time
    CPBL and the 2-week trend) agreed it was the weakest Meta performer, not just a
    thin-sample blip. Ad sets without enough data for a CPBL read sort last - cut a
    known-bad performer before an unknown one."""
    rows = []
    for name, d in meta_budgets.items():
        eff = meta_eff.get(name, {'spend': 0.0, 'bc': 0})
        cpbl = (eff['spend'] / eff['bc']) if eff['bc'] > 0 else None
        rows.append({'name': name, 'budget': d['daily_budget'], 'cpbl': cpbl, 'bc': eff['bc']})
    rows.sort(key=lambda r: (r['cpbl'] is None, -(r['cpbl'] or 0)))
    return rows


def rank_google_destinations(google_budgets, google_eff):
    """In-scope Google campaigns ranked best-CPBL-first. Unknown-CPBL sorts last -
    fund a proven performer before an unknown one."""
    rows = []
    for name, d in google_budgets.items():
        eff = google_eff.get(name, {'spend': 0.0, 'bc': 0})
        cpbl = (eff['spend'] / eff['bc']) if eff['bc'] > 0 else None
        rows.append({'name': name, 'budget': d['daily_budget'], 'cpbl': cpbl, 'bc': eff['bc']})
    rows.sort(key=lambda r: (r['cpbl'] is None, r['cpbl'] if r['cpbl'] is not None else 0))
    return rows


def allocate(target_rs, ranked_rows):
    """Greedily draw from (or fund into) ranked_rows in the order given, each
    capped at MAX_STEP_PCT of its own budget, until target_rs is met or the
    list is exhausted. Returns (allocations, total_allocated)."""
    remaining = target_rs
    allocations = []
    for r in ranked_rows:
        if remaining <= 0.5:
            break
        cap = r['budget'] * MAX_STEP_PCT
        amount = min(cap, remaining)
        if amount <= 0.5:
            continue
        allocations.append({'name': r['name'], 'amount': amount, 'cpbl': r['cpbl'], 'bc': r['bc']})
        remaining -= amount
    return allocations, target_rs - remaining


def pause_candidates(ranked_meta_sources):
    """Advisory only: Meta ad sets whose CPBL is >= PAUSE_CANDIDATE_MULT times the
    best in-scope Meta CPBL. Never sized into the automatic allocation - pausing an
    ad set entirely is always a human call."""
    known = [r for r in ranked_meta_sources if r['cpbl'] is not None]
    if not known: return []
    best = min(r['cpbl'] for r in known)
    return [r for r in known if r['cpbl'] >= best * PAUSE_CANDIDATE_MULT]


def check_monitoring(war_room, d1):
    """
    Flag if any metric breaches >20% on BOTH DoD AND same-day last week.
    Returns list of flag strings.
    """
    today_str     = d1.isoformat()
    yesterday_str = (d1 - datetime.timedelta(days=1)).isoformat()
    lastweek_str  = (d1 - datetime.timedelta(days=7)).isoformat()

    today     = war_room.get(today_str, {})
    yesterday = war_room.get(yesterday_str, {})
    lastweek  = war_room.get(lastweek_str, {})
    if not today or not yesterday or not lastweek:
        return []

    flags = []
    checks = [
        ('Spend',    'spend',    'dip'),
        ('Bookings', 'bookings', 'dip'),
        ('CPBL',     'cpbl',     'rise'),
    ]
    for label, field, direction in checks:
        v_today = today.get(field)
        v_yday  = yesterday.get(field)
        v_lw    = lastweek.get(field)
        if not v_today or not v_yday or not v_lw: continue
        dod = (v_today - v_yday) / v_yday
        wow = (v_today - v_lw)  / v_lw
        if direction == 'dip':
            if dod < -MONITORING_THRESH and wow < -MONITORING_THRESH:
                flags.append(f'{label}: -{abs(dod)*100:.0f}% DoD, -{abs(wow)*100:.0f}% WoW')
        else:
            if dod > MONITORING_THRESH and wow > MONITORING_THRESH:
                flags.append(f'{label}: +{dod*100:.0f}% DoD, +{wow*100:.0f}% WoW')
    return flags


# ---- message formatting ----

def fmt_rs(v): return f'Rs {v:,.0f}'
def fmt_cpbl(v): return f'{v:,.0f}' if v is not None else 'n/a (low volume)'


def msg_trigger(gap, consecutive, meta_cpbl, google_cpbl, target_rs,
                meta_allocs, google_allocs, pauses, step_n=1):
    total_meta = sum(a['amount'] for a in meta_allocs)
    total_goog = sum(a['amount'] for a in google_allocs)
    lines = [
        ':arrows_counterclockwise: *Budget Shift Pass* - *TRIGGER FIRES*',
        '',
        '*Channel CPBL (7-day rolling, Branch-attributed):*',
        f'  Meta: {fmt_rs(meta_cpbl)}  |  Google: {fmt_rs(google_cpbl)}',
        f'  Gap: {gap*100:.1f}%  >10% for {consecutive} consecutive days',
        '',
        f'*Step {step_n}: move up to {fmt_rs(target_rs)}/day - worst Meta CPBL first, '
        'into best Google CPBL first, each capped at 15% of its own budget:*',
        '',
        '*Reduce (Meta):*',
    ]
    for a in meta_allocs:
        lines.append(f'  `{a["name"]}`  -{fmt_rs(a["amount"])}/day  (CPBL {fmt_cpbl(a["cpbl"])})')
    if not meta_allocs:
        lines.append('  _no eligible Meta ad set found_')
    lines += ['', f'  Total reduced: {fmt_rs(total_meta)}/day', '', '*Fund (Google):*']
    for a in google_allocs:
        lines.append(f'  `{a["name"]}`  +{fmt_rs(a["amount"])}/day  (CPBL {fmt_cpbl(a["cpbl"])})')
    if not google_allocs:
        lines.append('  _no eligible Google campaign found_')
    lines += ['', f'  Total funded: {fmt_rs(total_goog)}/day']

    if pauses:
        lines += ['', ':bulb: *Pause candidates* '
                  f'(CPBL >= {PAUSE_CANDIDATE_MULT:.0f}x your best Meta ad set - '
                  'consider pausing entirely rather than just trimming; advisory only, not sized above):']
        for r in pauses:
            lines.append(f'  `{r["name"]}`  CPBL {fmt_cpbl(r["cpbl"])}  ({r["bc"]} bookings/7d)')

    lines += [
        '',
        '_All changes are manual. Adjust budgets in Meta Ads Manager and Google Ads console._',
        '',
        ':warning: _Incrementality caveat: sustained 40%+ gap may reflect attribution bleed '
        '(Meta drives demand, Google captures). Validate before committing to the full series (geo holdout pending)._',
    ]
    return '\n'.join(lines)


def msg_monitoring(step_n, flags, gap, next_step_date):
    if flags:
        lines = [
            f':bar_chart: *Budget Shift Pass* - monitoring (Step {step_n} in progress)',
            f'  Gap today: {gap*100:.1f}%',
            f'  :warning: *Monitoring flags raised - hold, review before next step ({next_step_date}):*',
        ]
        for f in flags: lines.append(f'    - {f}')
        lines.append('  _Assess whether movement is explained by the shift or other factors before proceeding._')
    else:
        lines = [
            f':white_check_mark: *Budget Shift Pass* - monitoring clean (Step {step_n} in progress)',
            f'  Gap today: {gap*100:.1f}%  |  No monitoring flags',
            f'  Next re-check: {next_step_date}',
        ]
    return '\n'.join(lines)


def msg_gap_closed(gap, step_n):
    return (f':white_check_mark: *Budget Shift Pass* - gap closed, stopping shift.\n'
            f'  Gap now {gap*100:.1f}% (at or below the {FAST_ITERATE_GAP*100:.0f}% fast-iterate threshold) after Step {step_n}.\n'
            f'  Entering 7-day stabilization window. No new shifts until stabilization completes.')


def msg_stabilization(days_remaining, gap):
    return (f':hourglass_flowing_sand: *Budget Shift Pass* - stabilization active.\n'
            f'  {days_remaining} day(s) remaining.  Gap today: {gap*100:.1f}%\n'
            f'  No new shifts until stabilization completes.')


def msg_stabilization_broken(gap, consecutive):
    return (f':rotating_light: *Budget Shift Pass* - stabilization broken early.\n'
            f'  Gap has been > {FAST_ITERATE_GAP*100:.0f}% for {consecutive} consecutive days '
            f'(today: {gap*100:.1f}%) - resuming iteration instead of waiting out the remaining window.')


def msg_stabilization_complete(gap):
    fires = gap > TRIGGER_GAP
    tail = f'  Gap is {gap*100:.1f}% - trigger {"will re-evaluate tomorrow" if fires else "below threshold, no action"}.'
    return (f':white_check_mark: *Budget Shift Pass* - stabilization complete.\n{tail}')


def msg_clean(gap, consecutive):
    return (f':white_check_mark: *Budget Shift Pass* - no trigger.\n'
            f'  Gap: {gap*100:.1f}%  |  Consecutive days >10%: {consecutive}/{TRIGGER_DAYS} needed')


# ---- CSV log ----

def append_log(date, gap_pct, shift_rs, source, destination, step_n, total_steps=None, note=''):
    write_header = not os.path.exists(ACTION_LOG_PATH) or os.path.getsize(ACTION_LOG_PATH) == 0
    with open(ACTION_LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(['date', 'trigger_gap_pct', 'shift_rs', 'source_channel',
                        'destination_channel', 'step_n', 'total_steps',
                        'execution_confirmed', 'execution_time', 'monitoring_flags', 'outcome_note'])
        w.writerow([date, f'{gap_pct*100:.1f}', f'{shift_rs:.0f}', source, destination,
                    step_n, total_steps or '', 'pending', '', '', note])


# ---- Slack ----

def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def slack_post(text, dm_only=False):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token:
        raise SystemExit('SLACK_BOT_TOKEN not set')
    dm = os.environ.get('SLACK_DM_USER_ID', SLACK_DM_DEFAULT)
    op = slack_api('conversations.open', token, {'users': dm})
    targets = []
    if op.get('ok'):
        targets.append(('DM', op['channel']['id']))
    if not dm_only:
        targets.append(('#growth-reports', os.environ.get('SLACK_CHANNEL_ID', SLACK_CHANNEL_DEFAULT)))
    for label, ch in targets:
        resp = slack_api('chat.postMessage', token, {'channel': ch, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
        print(f'posted to {label}:', 'ok' if resp.get('ok') else resp.get('error'))


# ---- shared: start a fresh shift from a firing trigger ----

def start_new_shift(gap, consecutive, meta_cpbl, google_cpbl, d1, run_time_ist, dry_run):
    """Builds step-1 allocations, saves state (unless dry_run), returns the Slack message."""
    meta_budgets   = get_meta_budgets()
    google_budgets = get_google_budgets()
    meta_total   = sum(d['daily_budget'] for d in meta_budgets.values() if d['type'] != 'RETARGETING')  # channel ceiling stays gap-driven, unrelated to which ad sets are eligible sources
    google_total = sum(d['daily_budget'] for d in google_budgets.values())
    meta_eff, google_eff = get_efficiency_data(d1)

    target_rs = compute_shift_target(gap, meta_total, google_total)
    meta_ranked  = rank_meta_sources(meta_budgets, meta_eff)
    google_ranked = rank_google_destinations(google_budgets, google_eff)
    meta_allocs, _  = allocate(target_rs, meta_ranked)
    google_allocs, _ = allocate(target_rs, google_ranked)
    pauses = pause_candidates(meta_ranked)

    next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
    state = {
        'phase': 'shift',
        'shift': {
            'initiated_date':  d1.isoformat(),
            'step':            1,
            'meta_allocations':   meta_allocs,
            'google_allocations': google_allocs,
            'trigger_gap_pct': round(gap * 100, 1),
            'last_step_date':  d1.isoformat(),
            'last_step_time':  run_time_ist.isoformat(),
            'next_step_date':  next_step_date,
        },
        'stabilization_end': None,
    }
    if not dry_run:
        save_state(state)
        src = '; '.join(f'{a["name"]} -{a["amount"]:.0f}' for a in meta_allocs)
        dst = '; '.join(f'{a["name"]} +{a["amount"]:.0f}' for a in google_allocs)
        append_log(d1.isoformat(), gap, sum(a['amount'] for a in meta_allocs), src, dst, 1)
    return msg_trigger(gap, consecutive, meta_cpbl, google_cpbl, target_rs,
                        meta_allocs, google_allocs, pauses, step_n=1)


# ---- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dm-only', action='store_true')
    ap.add_argument('--date', help='override anchor date YYYY-MM-DD')
    ap.add_argument('--reset', action='store_true', help='clear shift state and exit')
    args = ap.parse_args()
    load_env()

    if args.reset:
        save_state({'phase': 'none', 'shift': None, 'stabilization_end': None})
        print('State reset to none.')
        return

    # Proxy for "when the action was taken": the script only knows when IT ran,
    # not when the human actually applied the change on Meta/Google. Automated
    # cron runs land ~11:45 IST (pre-noon -> D1). If the real execution happens
    # later in the day, hand-edit `last_step_time` in budget_shift_state.json
    # before the next run to get the correct D0/D1 classification.
    run_time_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        d1 = (run_time_ist - datetime.timedelta(days=1)).date()

    state = load_state()
    war_room = get_war_room(d1)
    fires, gap, consecutive, meta_cpbl, google_cpbl = check_trigger(war_room, d1)
    msg = None
    phase = state.get('phase', 'none')

    # ---- stabilization ----
    if phase == 'stabilization':
        fast_fires, fast_gap, fast_consecutive = check_fast_iterate(war_room, d1)
        stab_end = datetime.date.fromisoformat(state['stabilization_end'])
        days_left = (stab_end - d1).days
        if fast_fires:
            # Gap re-widened past FAST_ITERATE_GAP for 3 consecutive days while
            # parked in stabilization - break out early and resume iterating
            # immediately, same run, rather than waiting for the window to lapse.
            broken_msg = msg_stabilization_broken(fast_gap or 0, fast_consecutive)
            if fires and gap is not None:
                started_msg = start_new_shift(gap, consecutive, meta_cpbl, google_cpbl, d1, run_time_ist, args.dry_run)
                msg = broken_msg + '\n\n' + started_msg
            else:
                if not args.dry_run:
                    save_state({'phase': 'none', 'shift': None, 'stabilization_end': None})
                msg = broken_msg
        elif days_left <= 0:
            state = {'phase': 'none', 'shift': None, 'stabilization_end': None}
            if not args.dry_run:
                save_state(state)
            msg = msg_stabilization_complete(gap or 0)
        else:
            msg = msg_stabilization(days_left, gap or 0)

    # ---- shift in progress ----
    elif phase == 'shift':
        shift = state['shift']
        step_n = shift['step']
        next_step = datetime.date.fromisoformat(shift['next_step_date'])
        flags = check_monitoring(war_room, d1)

        if d1 >= next_step:
            # Re-evaluate gap at step boundary. Cools to <= FAST_ITERATE_GAP(15%)
            # (not just below the base 10% trigger) -> settle into stabilization;
            # still above it -> keep iterating every STEP_CADENCE_DAYS.
            if gap is not None and gap <= FAST_ITERATE_GAP:
                stab_end = compute_stab_end(shift['last_step_date'], shift.get('last_step_time'))
                state = {'phase': 'stabilization', 'shift': None, 'stabilization_end': stab_end}
                if not args.dry_run:
                    save_state(state)
                msg = msg_gap_closed(gap, step_n)
            elif flags:
                msg = msg_monitoring(step_n, flags, gap or 0, next_step.isoformat())
                # Don't advance step; hold for human review
            else:
                meta_budgets   = get_meta_budgets()
                google_budgets = get_google_budgets()
                meta_total   = sum(d['daily_budget'] for d in meta_budgets.values() if d['type'] != 'RETARGETING')  # channel ceiling stays gap-driven, unrelated to which ad sets are eligible sources
                google_total = sum(d['daily_budget'] for d in google_budgets.values())
                meta_eff, google_eff = get_efficiency_data(d1)

                target_rs = compute_shift_target(gap, meta_total, google_total)
                meta_ranked   = rank_meta_sources(meta_budgets, meta_eff)
                google_ranked = rank_google_destinations(google_budgets, google_eff)
                meta_allocs, _   = allocate(target_rs, meta_ranked)
                google_allocs, _ = allocate(target_rs, google_ranked)
                pauses = pause_candidates(meta_ranked)

                next_step_n = step_n + 1
                next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
                state['shift']['step'] = next_step_n
                state['shift']['meta_allocations'] = meta_allocs
                state['shift']['google_allocations'] = google_allocs
                state['shift']['last_step_date'] = d1.isoformat()
                state['shift']['last_step_time'] = run_time_ist.isoformat()
                state['shift']['next_step_date'] = next_step_date
                if not args.dry_run:
                    save_state(state)
                    src = '; '.join(f'{a["name"]} -{a["amount"]:.0f}' for a in meta_allocs)
                    dst = '; '.join(f'{a["name"]} +{a["amount"]:.0f}' for a in google_allocs)
                    append_log(d1.isoformat(), gap, sum(a['amount'] for a in meta_allocs), src, dst, next_step_n)
                msg = msg_trigger(gap, consecutive, meta_cpbl, google_cpbl, target_rs,
                                  meta_allocs, google_allocs, pauses, step_n=next_step_n)
        else:
            # Between steps: monitoring only
            msg = msg_monitoring(step_n, flags, gap or 0, next_step.isoformat())

    # ---- no active shift: check trigger ----
    else:
        if fires and gap is not None:
            msg = start_new_shift(gap, consecutive, meta_cpbl, google_cpbl, d1, run_time_ist, args.dry_run)
        else:
            msg = msg_clean(gap or 0, consecutive)

    print(msg)
    if not args.dry_run:
        slack_post(msg, dm_only=args.dm_only)


if __name__ == '__main__':
    main()
