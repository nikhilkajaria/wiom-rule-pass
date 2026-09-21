# -*- coding: utf-8 -*-
"""Budget channel rebalancing pass -> Slack. Spec v2.3.

  Runs daily after kill pass (07:30 IST). v2.3 (2026-09-21, Nikhil - design
  locked after a same-day one-off analysis, see budget_shift_v23_design in
  chat history) REPLACES the entire v2.0-v2.2 channel-siloed, gap-triggered
  model below with a merged-pool, per-unit-threshold model:

  - NO channel siloing. Every in-scope Meta ad set and Google campaign is
    ranked together in one pool, regardless of channel. The old model only
    ever compared Meta's blended CPBL to Google's blended CPBL and moved
    money between the two channels as wholes - structurally blind to, e.g.,
    Google UAC being worse than Google DemandGen, or Meta RMKT being BETTER
    than Meta PBFC. v2.3 sees all of that.
  - NO gap-driven "direction." A unit is a SOURCE if its own 7-day CPBL is
    worse than blended paid CPBL (same 7-day window - see get_blended_cpbl
    for why window-matching matters), a DESTINATION if at/better than
    blended. This is symmetric and per-unit; there is no "Meta vs Google"
    direction to track, reverse, or contradict.
  - Sizing: each side is capped at MAX_STEP_PCT(15%) of its OWN budget, same
    anti-shock discipline as before. target_rs = min(total source capacity,
    total destination capacity) - i.e. sources are maxed out to their own
    15% ceiling, exactly as far as destinations can actually absorb, never
    further, never less (Nikhil, 2026-09-21: "sources must be maxed out
    till 15%... as long as destinations can afford it").
  - PROTECTED_FROM_SOURCING units (Mumbai, Bharat_Lucknow as of 2026-09-21 -
    "strategic investments right now") are never drawn from even if their
    own CPBL sits above blended. They remain fully eligible as destinations.
  - Trigger re-scoped (see check_trigger_v2): fires when at least one
    non-protected unit is worse than blended AND at least one unit is
    at/better than blended, for TRIGGER_DAYS(3) consecutive days. This is a
    much weaker bar than the old |channel gap|>10% trigger - in a multi-unit
    pool it's true almost every ordinary day, so this now behaves closer to
    a standing daily recommendation than an occasional alert. Flagged
    explicitly so the change in operating rhythm isn't a surprise.
  - DROPPED, not ported: DIRECTIONS/SOURCE_LABEL/DEST_LABEL, check_trigger,
    check_fast_iterate, compute_shift_target, msg_direction_contradicted,
    msg_shift_reversed. The direction-reversal/contradiction checks existed
    specifically to stop a multi-day shift from executing a new step against
    a direction today's data no longer supports - under v2.3 every step is
    already freshly reclassified from today's data, so there is no stale
    direction left to contradict.
  - KEPT unchanged: step cadence (STEP_CADENCE_DAYS), stabilization window
    (STABILIZATION_DAYS, compute_stab_end - pure date logic, never was
    gap-dependent), monitoring-flag hold (check_monitoring), pause
    candidates, per-unit 15% cap + neat-hundred rounding (allocate,
    _round_down_to_unit), ad-set age grace period, zero-booking-spend floor.

  budget_shift_state.json predating this redesign (missing 'schema': 'v2.3',
  or carrying an old 'direction' field) is treated as phase 'none' on load -
  see load_state. A shift "in progress" under the old channel-direction
  model has no equivalent under this one; continuing to track it would just
  be stale bookkeeping against a concept that no longer exists.

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

# ---- spec constants ----
TRIGGER_DAYS         = 3      # consecutive days of a real source/destination split to fire/override
MAX_STEP_PCT         = 0.15   # max budget change per step, PER ad set/campaign (both sides)
STEP_CADENCE_DAYS    = 1      # days between steps
STABILIZATION_DAYS   = 7      # days of read after a step finds no meaningful split
PAUSE_CANDIDATE_MULT = 2.0    # flag a source as a pause candidate at >= this x the best source CPBL
# v2.1 (2026-08-14, Nikhil): a zero-BC ad set was sorting to the BACK of worst-CPBL-first -
# same "insufficient data" bucket as a genuinely fresh ad set - because cpbl=None for both.
# Confirmed live: BHARAT_LUCKNOW spent Rs9,197/7d (0 lifetime bookings since launch, 5-6 days
# old - not a thin-sample blip) and never once qualified as a reduce source or a pause
# candidate, because pause_candidates() also filters out cpbl is None before flagging. Real
# spend + zero bookings is the single clearest bad-performer signal an ad set can produce -
# it must never be treated as "unknown, judge later." Same principle as ZERO_BC_SPEND in
# rule_pass.py. Below this floor, cpbl stays None (genuinely too new/thin to judge, sorts
# last as before) - only real, meaningful spend with zero return gets treated as confirmed-worst.
ZERO_BOOKING_SPEND_FLOOR = 5000  # Rs 7-day spend; 0 BC above this = confirmed worst (inf), not unknown
# v2.3 (2026-08-25, Nikhil): a brand-new, deliberately-launched ad set (e.g. a strategic
# geo test) was showing up as a reduce SOURCE on day one, before it could possibly have
# earned bookings - punished for being new, not for being bad. Ad sets younger than this
# are excluded entirely from rank_worst_cpbl_first, regardless of spend or CPBL.
# TENSION, read before changing: this partially undoes the 2026-08-14 ZERO_BOOKING_SPEND_FLOOR
# fix above, whose own worked example was a 5-6-day-old BHARAT_LUCKNOW ad set that Nikhil
# explicitly ruled should NOT get a pass ("real spend + zero bookings is the single clearest
# bad-performer signal ... must never be treated as unknown, judge later"). At 7 days here,
# that same-aged case would now be exempted - the two decisions genuinely conflict, not just
# in appearance. Kept at 7 to match rule_pass.py's AGE_GRACE_DAYS convention, but this is a
# judgment call Nikhil made once and is now making differently for a specific strategic ad
# set - if a future non-strategic new ad set starts bleeding real spend for a full week under
# this grace period, that's this exact tension resurfacing, not a new bug.
AD_SET_AGE_GRACE_DAYS = 7
MONITORING_THRESH    = 0.20   # flag if metric moves >20% on both DoD and WoW
DASH_BASE            = 'https://growth-portal.up.railway.app'
META_ACC_DEFAULT     = '2007675312900454'
META_VER_DEFAULT     = 'v23.0'
GOOGLE_CID_DEFAULT   = '1218037894'
SLACK_CHANNEL_DEFAULT = 'C0C216DU0P6'  # #demand-reports (repointed 2026-09-15, currently dormant - this script runs --dm-only in production)
SLACK_DM_DEFAULT      = 'U05A9037VFG'  # Nikhil

# in-scope campaign name substrings (case-insensitive)
META_IN_SCOPE  = ['BFC-VOLUME', 'RETARGETING']
GOOGLE_IN_SCOPE = ['UAC', 'DEMANDGEN', 'SEARCH']
GOOGLE_TOF_EXCLUDE = ['AWARENESS']  # exclude ToF from budget pool

# v2.3 (2026-09-21, Nikhil): "protected as strategic investments right now" - never sourced
# from, even if their own CPBL sits above blended. Still fully eligible as destinations.
# A short-lived, manually-maintained list, not a durable campaign-type rule like META_IN_SCOPE -
# revisit when the strategic call changes.
PROTECTED_FROM_SOURCING = {
    'MUMBAI_CSP99_SPL_L1-L2-L3_BROAD_MULTI_APPSTORE_ABO_BFC-VOLUME_MULTI_NA',
    'BHARAT_LUCKNOW_AI_L1-L2-L3_BROAD_BOOKNOW_APPSTORE_ABO_BFC-VOLUME_MULTI_NA',
}

STATE_SCHEMA = 'v2.3'

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH      = os.path.join(_DIR, 'budget_shift_state.json')
ACTION_LOG_PATH = os.path.join(_DIR, 'budget_shift_log.csv')
# v2.3: Meta/Google's own "created" field is the ad set/campaign OBJECT's original creation
# date, not when it started running under its current name/purpose - ad set shells get
# renamed and repurposed rather than recreated (confirmed live 2026-08-25: BHARAT_LUCKNOW_AI
# reused a shell created 2026-08-08, but had zero spend/bookings as of the swap - genuinely
# brand new operationally, API said 17 days old). Manual override, same pattern as
# rule_pass.py's activation-state backfill: {ad_set_or_campaign_name: 'YYYY-MM-DD'}, checked
# before falling back to the API's created_time/start_date.
AD_SET_AGE_OVERRIDE_PATH = os.path.join(_DIR, 'ad_set_age_overrides.json')


def _load_age_overrides():
    if os.path.exists(AD_SET_AGE_OVERRIDE_PATH):
        try:
            with open(AD_SET_AGE_OVERRIDE_PATH, encoding='utf-8') as f:
                raw = json.load(f)
            return {k: datetime.date.fromisoformat(v) for k, v in raw.items()}
        except Exception:
            pass
    return {}


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
    """v2.3: a state file predating this redesign (no 'schema': 'v2.3' tag - includes every
    file written by v2.0-v2.2, which used a 'direction' field instead) is treated as phase
    'none'. A shift "in progress" under the old channel-direction model (e.g. the real
    GOOGLE_TO_META step-1 state pending as of 2026-09-20) has no equivalent concept here -
    continuing to track its step/next_step_date would just be stale bookkeeping against a
    model that no longer exists, not a safe resume."""
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                state = json.load(f)
            if state.get('schema') != STATE_SCHEMA:
                print("note: state file predates v2.3 (channel-direction shifts no longer exist) - resetting to phase 'none'")
                return {'schema': STATE_SCHEMA, 'phase': 'none', 'shift': None, 'stabilization_end': None}
            return state
        except Exception as e:
            print(f'warn: could not read state - {e}')
    return {'schema': STATE_SCHEMA, 'phase': 'none', 'shift': None, 'stabilization_end': None}


def save_state(state):
    state = dict(state)
    state['schema'] = STATE_SCHEMA
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


def get_blended_cpbl(war_room, d1, lookback=6):
    """Blended paid CPBL (BOF spend / BOF confirmed bookings), over the EXACT SAME trailing
    window as get_efficiency_data's per-unit CPBL: start=d1-lookback, end=d1 inclusive -
    lookback=6 is a 7-day window, not 6 (matches get_efficiency_data's own docstring). Caught
    live 2026-09-21: an earlier one-off analysis compared unit-level 7-day CPBL against a
    SINGLE-DAY blended figure (that day's own overall_cpbl), which silently favored whichever
    window had the better days - Sep 20 alone read Rs468 (an unusually good day) vs the
    correct 7-day trailing Rs648.5, which misclassified DEL_ALL_PBFC as a destination instead
    of a source. ALWAYS call with the same lookback passed to get_efficiency_data for the same
    d1, or the two numbers are silently not comparable. Returns None if no bookings in window."""
    dates = [d1 - datetime.timedelta(days=i) for i in range(lookback + 1)]
    spend = sum(war_room.get(d.isoformat(), {}).get('bof_spend') or 0 for d in dates)
    bc = sum(war_room.get(d.isoformat(), {}).get('bof_confirmed') or 0 for d in dates)
    return (spend / bc) if bc else None


def get_efficiency_data(d1, lookback=6):
    """
    7-day spend + booking_confirmed per Meta ad_set and Google campaign, via the
    raw /api/raw/days attribution[] + meta_spend[] endpoints - NOT master_export,
    whose Google-side booking_confirmed field reads 0 for every row (confirmed
    2026-07-11). Google is aggregated at the CAMPAIGN level throughout: Search's
    attribution rows only populate `campaign` (ad_set is blank there), while
    UAC/DemandGen already collapse ad_set==campaign - campaign is the one join
    key that works for every Google campaign type.
    lookback=6 -> window is [d1-6, d1] INCLUSIVE, i.e. 7 calendar days, matching the
    "7-day" in this docstring - see get_blended_cpbl for why this matters and how to
    keep a blended-CPBL comparison window-matched to this.
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

    overrides = _load_age_overrides()
    budgets = {}  # adset_name -> {id, daily_budget, campaign_type, created_date}
    for c in camps:
        if c.get('effective_status') != 'ACTIVE': continue
        cname = c.get('name', '').upper()
        ctype = next((t for t in META_IN_SCOPE if t in cname), None)
        if not ctype: continue
        url2 = (f'https://graph.facebook.com/{ver}/{c["id"]}/adsets?'
                f'fields=id,name,effective_status,daily_budget,created_time&limit=100&access_token={tok}')
        with urllib.request.urlopen(url2, timeout=30) as r:
            adsets = json.loads(r.read().decode()).get('data', [])
        for a in adsets:
            if a.get('effective_status') != 'ACTIVE': continue
            db = int(a.get('daily_budget') or 0) / 100
            if db > 0:
                if a['name'] in overrides:
                    created_date = overrides[a['name']]
                else:
                    created_date = None
                    ct = a.get('created_time')
                    if ct:
                        try: created_date = datetime.date.fromisoformat(ct[:10])
                        except Exception: created_date = None
                budgets[a['name']] = {'id': a['id'], 'daily_budget': db, 'type': ctype, 'created_date': created_date}
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
        # v2.3 REGRESSION (2026-08-25 23:15 -> found 2026-08-30): 'campaign.start_date' is not
        # a valid queryable field in this account's GAQL schema ("Unrecognized field in the
        # query"). Google Ads raises on the WHOLE query when this happens, and the broad
        # except below swallows it and returns {} - so this single bad field silently zeroed
        # out the entire Google budget pool for every run from 2026-08-26 onward (confirmed:
        # the Aug25 23:15 IST commit that added this field is the last commit before the very
        # next scheduled run, Aug26 16:10 IST, started posting "Rs0/day, no eligible ad
        # set/campaign found" - a real, on-platform-looking result that was actually a fetch
        # failure, not a legitimate zero). Dropped the field entirely rather than hunt for the
        # "correct" name under time pressure - Google-side campaigns just get created_date=None
        # (no automatic age-grace signal) unless manually set via ad_set_age_overrides.json,
        # same as before this feature existed. Meta's created_time fetch is unaffected (only
        # this Google query used start_date) and keeps working as designed.
        query = '''SELECT campaign.id, campaign.name, campaign_budget.amount_micros
                   FROM campaign WHERE campaign.status = ENABLED
                   ORDER BY campaign_budget.amount_micros DESC'''
        overrides = _load_age_overrides()
        budgets = {}
        for row in ga.search(customer_id=cid, query=query):
            name  = row.campaign.name
            name_up = name.upper()
            if any(ex in name_up for ex in GOOGLE_TOF_EXCLUDE): continue
            ctype = next((t for t in GOOGLE_IN_SCOPE if t in name_up), None)
            if not ctype: continue
            daily_rs = row.campaign_budget.amount_micros / 1_000_000
            created_date = overrides.get(name)
            budgets[name] = {'id': str(row.campaign.id), 'daily_budget': daily_rs, 'type': ctype, 'created_date': created_date}
        return budgets
    except Exception as e:
        print(f'warn: Google Ads budgets unavailable - {e}')
        return {}


# ---- trigger logic (v2.3: merged-pool, per-unit threshold - see module docstring) ----

def check_trigger_v2(war_room, d1, lookback=6):
    """Re-scoped from the old channel-gap trigger (v2.0-v2.2): fires when at least one
    non-protected unit prices worse than blended CPBL (a real source) AND at least one unit
    prices at/better than blended (a real destination), for TRIGGER_DAYS(3) consecutive days.
    Dashboard-only (get_blended_cpbl + get_efficiency_data, never get_meta_budgets/
    get_google_budgets) - cheap, no live Meta/Google Ads API calls, safe to run once per day
    before touching ad-platform rate limits at all.

    NOTE (2026-09-21): unlike the old channel-level gap, which was a rare, alarming >10%
    divergence between two blended numbers, "some unit is above blended and some unit is
    below" is true on almost every ordinary day in a multi-unit pool - this fires far more
    often than the old trigger did, closer to a standing daily recommendation than an
    occasional alert. That's an intentional consequence of moving to per-unit granularity,
    not a bug - flagged here so it isn't mistaken for one.

    This does NOT apply AD_SET_AGE_GRACE_DAYS filtering (no budget/created_date data at this
    stage) - that's fine for a lightweight streak gate; the real step computation
    (classify_and_allocate) re-applies full filtering via rank_worst_cpbl_first.

    Returns (fires, consecutive_days, blended_cpbl_today)."""
    consecutive = 0
    blended_today = None
    for i in range(TRIGGER_DAYS - 1, -1, -1):
        d = d1 - datetime.timedelta(days=i)
        blended = get_blended_cpbl(war_room, d, lookback)
        meta_eff, google_eff = get_efficiency_data(d, lookback)
        eff = dict(meta_eff)
        eff.update(google_eff)
        if blended is None:
            consecutive = 0
        else:
            has_source = any(
                e['bc'] > 0 and (e['spend'] / e['bc']) > blended and name not in PROTECTED_FROM_SOURCING
                for name, e in eff.items())
            has_dest = any(e['bc'] > 0 and (e['spend'] / e['bc']) <= blended for e in eff.values())
            consecutive = consecutive + 1 if (has_source and has_dest) else 0
        if i == 0:
            blended_today = blended
    return consecutive >= TRIGGER_DAYS, consecutive, blended_today


def compute_stab_end(last_step_date_str, last_step_time_str):
    """
    7-day stabilization window anchored to the LAST REAL shift (not to whatever
    day a later step-boundary check happens to notice the split already closed).

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


# ---- sizing + distribution (v2.3: merged pool, threshold-classified) ----

def rank_worst_cpbl_first(budgets, eff, d1=None):
    """In-scope ad sets/campaigns ranked worst-CPBL-first, across BOTH channels merged into
    one pool (v2.3 - see module docstring; genericized across channels 2026-08-20, merged into
    a single pool 2026-09-21). Ad sets/campaigns without enough data for a CPBL read sort last
    - cut a known-bad performer before an unknown one.

    Zero-BC entries are NOT automatically "insufficient data" (see ZERO_BOOKING_SPEND_FLOOR
    note above) - above the spend floor, zero bookings on real spend is confirmed-worst
    (cpbl=inf, sorts FIRST), not unknown (cpbl=None, sorts last). Below the floor, still
    genuinely too thin to judge - unchanged.

    v2.3: ad sets younger than AD_SET_AGE_GRACE_DAYS (by created_date) are excluded from this
    ranking entirely - see the tension noted at that constant's definition before changing it.
    d1=None (no date to judge age against) skips the age filter rather than excluding
    everyone - callers that don't pass d1 get the pre-age-filter behavior."""
    rows = []
    for name, d in budgets.items():
        if d1 and d.get('created_date') and (d1 - d['created_date']).days < AD_SET_AGE_GRACE_DAYS:
            continue
        e = eff.get(name, {'spend': 0.0, 'bc': 0})
        if e['bc'] > 0:
            cpbl = e['spend'] / e['bc']
        elif e['spend'] >= ZERO_BOOKING_SPEND_FLOOR:
            cpbl = float('inf')
        else:
            cpbl = None
        rows.append({'name': name, 'budget': d['daily_budget'], 'cpbl': cpbl, 'bc': e['bc']})
    rows.sort(key=lambda r: (r['cpbl'] is None, -(r['cpbl'] or 0)))
    return rows


def rank_best_cpbl_first(budgets, eff):
    """In-scope ad sets/campaigns ranked best-CPBL-first, across BOTH channels merged into one
    pool (v2.3). Unknown-CPBL sorts last - fund a proven performer before an unknown one."""
    rows = []
    for name, d in budgets.items():
        e = eff.get(name, {'spend': 0.0, 'bc': 0})
        cpbl = (e['spend'] / e['bc']) if e['bc'] > 0 else None
        rows.append({'name': name, 'budget': d['daily_budget'], 'cpbl': cpbl, 'bc': e['bc']})
    rows.sort(key=lambda r: (r['cpbl'] is None, r['cpbl'] if r['cpbl'] is not None else 0))
    return rows


ROUND_UNIT_SMALL = 50    # for an ad set/campaign whose OWN current budget is itself "in hundreds"
ROUND_UNIT_LARGE = 100   # for everything else (budget already in the thousands+)
ROUND_UNIT_SMALL_CEILING = 1000  # budget below this = "in hundreds" -> use the 50 unit


def _round_down_to_unit(amount, own_budget):
    """Nikhil has been manually rounding every step's line items to neat hundreds
    (or fifties, for a small-budget ad set/campaign where a flat 100 would be too
    coarse a step relative to its own scale) before entering them in Ads Manager -
    see manual_budget_changes.csv history (e.g. 2026-08-14: 'Applied as -1,250
    (rounded), pass recommended -1,462'). Built into the pass itself (2026-09-05,
    Nikhil) so the output is already what gets typed in, not a number that still
    needs hand-rounding every time. Always rounds the MAGNITUDE down (toward
    zero) - matches the established pattern of always being a little less
    aggressive than the precise computed number, never more."""
    unit = ROUND_UNIT_SMALL if own_budget < ROUND_UNIT_SMALL_CEILING else ROUND_UNIT_LARGE
    return (amount // unit) * unit


def allocate(target_rs, ranked_rows):
    """Greedily draw from (or fund into) ranked_rows in the order given, each
    capped at MAX_STEP_PCT of its own budget, until target_rs is met or the
    list is exhausted. Each allocation is rounded down to a neat unit (see
    _round_down_to_unit) before being counted against target_rs, so a rounded-
    to-zero allocation is skipped rather than appearing as a no-op line, and the
    running total reflects what's actually being recommended, not the raw
    pre-rounding figure. Returns (allocations, total_allocated)."""
    remaining = target_rs
    allocations = []
    for r in ranked_rows:
        if remaining <= 0.5:
            break
        cap = r['budget'] * MAX_STEP_PCT
        amount = min(cap, remaining)
        if amount <= 0.5:
            continue
        amount = _round_down_to_unit(amount, r['budget'])
        if amount <= 0:
            continue
        allocations.append({'name': r['name'], 'amount': amount, 'cpbl': r['cpbl'], 'bc': r['bc']})
        remaining -= amount
    return allocations, target_rs - remaining


def pause_candidates(ranked_source):
    """Advisory only: source-pool ad sets/campaigns whose CPBL is >=
    PAUSE_CANDIDATE_MULT times the best in-scope source-pool CPBL. Never sized
    into the automatic allocation - pausing an ad set/campaign entirely is always
    a human call."""
    known = [r for r in ranked_source if r['cpbl'] is not None]
    if not known: return []
    best = min(r['cpbl'] for r in known)
    return [r for r in known if r['cpbl'] >= best * PAUSE_CANDIDATE_MULT]


def classify_and_allocate(d1, lookback=6):
    """v2.3 core. Merged Meta+Google pool, no channel siloing, no gap-driven direction. A unit
    is a SOURCE if its own CPBL is worse (higher) than blended paid CPBL over the SAME window
    (see get_blended_cpbl); a DESTINATION if at/better than blended. PROTECTED_FROM_SOURCING
    units are never sources (still eligible as destinations). Each side is capped at its own
    MAX_STEP_PCT; target_rs = min(total source capacity, total destination capacity) - sources
    are maxed out to their own 15% ceiling, exactly as far as destinations can actually absorb.

    Live Meta/Google Ads API calls happen ONCE here (get_meta_budgets/get_google_budgets) -
    check_trigger_v2's multi-day streak check deliberately does NOT call this; it uses
    get_blended_cpbl + get_efficiency_data directly (dashboard-only) so a 3-day trigger
    evaluation never costs 3x the live ad-platform read budget.

    Returns (blended_cpbl, target_rs, source_allocs, dest_allocs, pauses, source_ranked)."""
    war_room = get_war_room(d1)
    blended_cpbl = get_blended_cpbl(war_room, d1, lookback)

    meta_budgets = get_meta_budgets()
    google_budgets = get_google_budgets()
    merged_budgets = dict(meta_budgets)
    merged_budgets.update(google_budgets)
    meta_eff, google_eff = get_efficiency_data(d1, lookback)
    merged_eff = dict(meta_eff)
    merged_eff.update(google_eff)

    worst_ranked = rank_worst_cpbl_first(merged_budgets, merged_eff, d1)
    best_ranked = rank_best_cpbl_first(merged_budgets, merged_eff)

    if blended_cpbl is None:
        return blended_cpbl, 0, [], [], [], worst_ranked

    source_ranked = [r for r in worst_ranked
                      if r['cpbl'] is not None and r['cpbl'] > blended_cpbl
                      and r['name'] not in PROTECTED_FROM_SOURCING]
    dest_ranked = [r for r in best_ranked
                   if r['cpbl'] is not None and r['cpbl'] <= blended_cpbl]

    source_capacity = sum(_round_down_to_unit(r['budget'] * MAX_STEP_PCT, r['budget']) for r in source_ranked)
    dest_capacity = sum(_round_down_to_unit(r['budget'] * MAX_STEP_PCT, r['budget']) for r in dest_ranked)
    target_rs = min(source_capacity, dest_capacity)

    source_allocs, _ = allocate(target_rs, source_ranked)
    dest_allocs, _ = allocate(target_rs, dest_ranked)
    pauses = pause_candidates(source_ranked)
    return blended_cpbl, target_rs, source_allocs, dest_allocs, pauses, source_ranked


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

def fmt_rs(v):
    if v is None: return 'n/a'
    return f'Rs {v:,.0f}'
def fmt_cpbl(v):
    if v is None: return 'n/a (low volume)'
    if v == float('inf'): return 'zero bookings on real spend'
    return f'{v:,.0f}'


def msg_trigger(blended_cpbl, consecutive, target_rs, source_allocs, dest_allocs, pauses, step_n=1):
    """v2.3: no more channel labels/direction - source_allocs/dest_allocs are drawn from the
    merged pool, classified purely by each unit's own CPBL against blended."""
    total_source = sum(a['amount'] for a in source_allocs)
    total_dest = sum(a['amount'] for a in dest_allocs)
    lines = [
        ':arrows_counterclockwise: *Budget Shift Pass* - *TRIGGER FIRES*',
        '',
        f'*Blended paid CPBL (7-day rolling, BOF spend/confirmed): {fmt_rs(blended_cpbl)}*',
        f'  Sources priced above this, destinations at/below it - held {consecutive} consecutive day(s)',
        '',
        f'*Step {step_n}: sources maxed to 15% of their own budget, capped by what destinations '
        f'can absorb at their own 15% - up to {fmt_rs(target_rs)}/day, worst CPBL sourced first, '
        f'best CPBL funded first:*',
        '',
        '*Reduce (sources, CPBL above blended):*',
    ]
    for a in source_allocs:
        lines.append(f'  `{a["name"]}`  -{fmt_rs(a["amount"])}/day  (CPBL {fmt_cpbl(a["cpbl"])})')
    if not source_allocs:
        lines.append('  _no eligible source ad set/campaign found_')
    lines += ['', f'  Total reduced: {fmt_rs(total_source)}/day', '', '*Fund (destinations, CPBL at/below blended):*']
    for a in dest_allocs:
        lines.append(f'  `{a["name"]}`  +{fmt_rs(a["amount"])}/day  (CPBL {fmt_cpbl(a["cpbl"])})')
    if not dest_allocs:
        lines.append('  _no eligible destination ad set/campaign found_')
    lines += ['', f'  Total funded: {fmt_rs(total_dest)}/day']

    if pauses:
        lines += ['', ':bulb: *Pause candidates* '
                  f'(CPBL >= {PAUSE_CANDIDATE_MULT:.0f}x the best source CPBL - '
                  'consider pausing entirely rather than just trimming; advisory only, not sized above):']
        for r in pauses:
            lines.append(f'  `{r["name"]}`  CPBL {fmt_cpbl(r["cpbl"])}  ({r["bc"]} bookings/7d)')

    lines += [
        '',
        '_All changes are manual. Adjust budgets in Meta Ads Manager and Google Ads console._',
        '',
        ':warning: _Cross-channel note: a Meta<->Google move can partly reflect attribution-window '
        'differences between platforms, not just real performance. Validate before committing to the '
        'full series (geo holdout pending)._',
    ]
    return '\n'.join(lines)


def msg_monitoring(step_n, flags, blended_cpbl, next_step_date):
    if flags:
        lines = [
            f':bar_chart: *Budget Shift Pass* - monitoring (Step {step_n} in progress)',
            f'  Blended CPBL today: {fmt_rs(blended_cpbl)}',
            f'  :warning: *Monitoring flags raised - hold, review before next step ({next_step_date}):*',
        ]
        for f in flags: lines.append(f'    - {f}')
        lines.append('  _Assess whether movement is explained by the shift or other factors before proceeding._')
    else:
        lines = [
            f':white_check_mark: *Budget Shift Pass* - monitoring clean (Step {step_n} in progress)',
            f'  Blended CPBL today: {fmt_rs(blended_cpbl)}  |  No monitoring flags',
            f'  Next re-check: {next_step_date}',
        ]
    return '\n'.join(lines)


def msg_stand_down(step_n):
    """v2.3, replaces msg_gap_closed: fires when a step finds no meaningful source/destination
    split left (target_rs rounds to 0, or one side is empty) - the per-unit equivalent of the
    old 'gap closed' condition."""
    return (f':white_check_mark: *Budget Shift Pass* - no meaningful split today, stopping shift.\n'
            f'  Step {step_n} found no room to move (source/destination capacity netted to Rs0, or '
            f'one side was empty).\n'
            f'  Entering {STABILIZATION_DAYS}-day stabilization window. No new shifts until stabilization completes.')


def msg_stabilization(days_remaining, blended_cpbl):
    return (f':hourglass_flowing_sand: *Budget Shift Pass* - stabilization active.\n'
            f'  {days_remaining} day(s) remaining. Blended CPBL today: {fmt_rs(blended_cpbl)}\n'
            f'  No new shifts until stabilization completes.')


def msg_stabilization_broken(consecutive):
    """v2.3, replaces msg_stabilization_broken's gap-based trigger: fires when a real
    source/destination split has re-confirmed for TRIGGER_DAYS days while parked."""
    return (f':rotating_light: *Budget Shift Pass* - stabilization broken early.\n'
            f'  A real source/destination split has held for {consecutive} consecutive day(s) while '
            f'parked - resuming iteration instead of waiting out the remaining window.')


def msg_stabilization_complete():
    return ':white_check_mark: *Budget Shift Pass* - stabilization complete. Re-evaluating trigger tomorrow.'


def msg_clean(consecutive):
    return (f':white_check_mark: *Budget Shift Pass* - no trigger.\n'
            f'  Consecutive days with a real source/destination split: {consecutive}/{TRIGGER_DAYS} needed')


# ---- CSV log ----

def append_log(date, blended_cpbl, shift_rs, source, destination, step_n, total_steps=None, note=''):
    """Column names kept as-is for continuity with the pre-v2.3 log (trigger_gap_pct/
    source_channel/destination_channel) - values are now blended CPBL and per-unit allocation
    strings instead of a channel gap % and channel labels. Existing historical rows are
    untouched; only what gets written into these columns going forward has changed meaning."""
    write_header = not os.path.exists(ACTION_LOG_PATH) or os.path.getsize(ACTION_LOG_PATH) == 0
    with open(ACTION_LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(['date', 'trigger_gap_pct', 'shift_rs', 'source_channel',
                        'destination_channel', 'step_n', 'total_steps',
                        'execution_confirmed', 'execution_time', 'monitoring_flags', 'outcome_note'])
        w.writerow([date, f'{blended_cpbl:.0f}', f'{shift_rs:.0f}', source, destination,
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
        targets.append(('#demand-reports', os.environ.get('SLACK_CHANNEL_ID', SLACK_CHANNEL_DEFAULT)))
    for label, ch in targets:
        resp = slack_api('chat.postMessage', token, {'channel': ch, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
        print(f'posted to {label}:', 'ok' if resp.get('ok') else resp.get('error'))


# ---- shared: start a fresh shift from a firing trigger ----

def start_new_shift(consecutive, d1, run_time_ist, dry_run):
    """Builds step-1 allocations, saves state (unless dry_run), returns the Slack message."""
    blended_cpbl, target_rs, source_allocs, dest_allocs, pauses, _ = classify_and_allocate(d1)

    next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
    state = {
        'phase': 'shift',
        'shift': {
            'initiated_date':   d1.isoformat(),
            'step':             1,
            'source_allocations': source_allocs,
            'dest_allocations':   dest_allocs,
            'blended_cpbl':     round(blended_cpbl, 1) if blended_cpbl is not None else None,
            'last_step_date':   d1.isoformat(),
            'last_step_time':   run_time_ist.isoformat(),
            'next_step_date':   next_step_date,
        },
        'stabilization_end': None,
    }
    if not dry_run:
        save_state(state)
        src = '; '.join(f'{a["name"]} -{a["amount"]:.0f}' for a in source_allocs)
        dst = '; '.join(f'{a["name"]} +{a["amount"]:.0f}' for a in dest_allocs)
        append_log(d1.isoformat(), blended_cpbl or 0, target_rs, src, dst, 1)
    return msg_trigger(blended_cpbl, consecutive, target_rs, source_allocs, dest_allocs, pauses, step_n=1)


# ---- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--dm-only', action='store_true')
    ap.add_argument('--date', help='override anchor date YYYY-MM-DD')
    ap.add_argument('--reset', action='store_true', help='clear shift state and exit')
    ap.add_argument('--no-post', action='store_true',
                     help='run for real (write state/log) but skip Slack - for backfilling on corrected '
                          'data without re-notifying; print the message instead of posting it')
    ap.add_argument('--last-retry', action='store_true',
                     help='final scheduled attempt of the day - alert if dashboard data is still not ready, '
                          'instead of quietly postponing to the next retry')
    args = ap.parse_args()
    load_env()

    if args.reset:
        save_state({'phase': 'none', 'shift': None, 'stabilization_end': None})
        print('State reset to none.')
        return

    # Proxy for "when the action was taken": the script only knows when IT ran,
    # not when the human actually applied the change on Meta/Google. Automated
    # cron runs land ~13:30 IST (pre-noon -> D1). If the real execution happens
    # later in the day, hand-edit `last_step_time` in budget_shift_state.json
    # before the next run to get the correct D0/D1 classification.
    run_time_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        d1 = (run_time_ist - datetime.timedelta(days=1)).date()

    # Dashboard-readiness gate (v2.1, 2026-07-17) - see dashboard_readiness.py
    # for why and daily-rule-pass.yml/budget-shift-pass.yml for the
    # 13:30/15:30/17:30 IST retry triggers. Skipped for --dry-run/--date -
    # those are intentional manual actions, not the unattended schedule.
    if not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today, mark_completed_today
        if already_completed_today('budget_shift_pass', d1):
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
                        f":rotating_light: *Budget Shift Pass* - dashboard data for {d1} still incomplete "
                        f"after 3 attempts (dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). "
                        f"Pass did NOT run today - check the dashboard ETL.",
                        dm_only=args.dm_only)
                print(f'last retry - data still not ready for {d1} (dashboard={dash_s}, actual={act_s}) - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    state = load_state()
    war_room = get_war_room(d1)
    fires, consecutive, blended_cpbl_today = check_trigger_v2(war_room, d1)
    msg = None
    phase = state.get('phase', 'none')

    # ---- stabilization ----
    if phase == 'stabilization':
        fast_fires, fast_consecutive, _ = check_trigger_v2(war_room, d1)
        stab_end = datetime.date.fromisoformat(state['stabilization_end'])
        days_left = (stab_end - d1).days
        if fast_fires:
            # A real source/destination split has re-confirmed for TRIGGER_DAYS days while
            # parked - break out early and resume iterating immediately, same run, rather
            # than waiting for the window to lapse.
            broken_msg = msg_stabilization_broken(fast_consecutive)
            started_msg = start_new_shift(consecutive, d1, run_time_ist, args.dry_run)
            msg = broken_msg + '\n\n' + started_msg
        elif days_left <= 0:
            state = {'phase': 'none', 'shift': None, 'stabilization_end': None}
            if not args.dry_run:
                save_state(state)
            msg = msg_stabilization_complete()
        else:
            msg = msg_stabilization(days_left, blended_cpbl_today)

    # ---- shift in progress ----
    elif phase == 'shift':
        shift = state['shift']
        step_n = shift['step']
        next_step = datetime.date.fromisoformat(shift['next_step_date'])
        flags = check_monitoring(war_room, d1)

        if d1 >= next_step:
            if flags:
                msg = msg_monitoring(step_n, flags, blended_cpbl_today, next_step.isoformat())
                # Don't advance step; hold for human review
            else:
                blended_cpbl, target_rs, source_allocs, dest_allocs, pauses, _ = classify_and_allocate(d1)

                if target_rs <= 0 or (not source_allocs and not dest_allocs):
                    # v2.3 equivalent of "gap closed": no meaningful split left to act on.
                    stab_end = compute_stab_end(shift['last_step_date'], shift.get('last_step_time'))
                    state = {'phase': 'stabilization', 'shift': None, 'stabilization_end': stab_end}
                    if not args.dry_run:
                        save_state(state)
                    msg = msg_stand_down(step_n)
                else:
                    next_step_n = step_n + 1
                    next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
                    state['shift']['step'] = next_step_n
                    state['shift']['source_allocations'] = source_allocs
                    state['shift']['dest_allocations'] = dest_allocs
                    state['shift']['blended_cpbl'] = round(blended_cpbl, 1) if blended_cpbl is not None else None
                    state['shift']['last_step_date'] = d1.isoformat()
                    state['shift']['last_step_time'] = run_time_ist.isoformat()
                    state['shift']['next_step_date'] = next_step_date
                    if not args.dry_run:
                        save_state(state)
                        src = '; '.join(f'{a["name"]} -{a["amount"]:.0f}' for a in source_allocs)
                        dst = '; '.join(f'{a["name"]} +{a["amount"]:.0f}' for a in dest_allocs)
                        append_log(d1.isoformat(), blended_cpbl or 0, target_rs, src, dst, next_step_n)
                    msg = msg_trigger(blended_cpbl, consecutive, target_rs, source_allocs, dest_allocs,
                                       pauses, step_n=next_step_n)
        else:
            # Between steps: monitoring only
            msg = msg_monitoring(step_n, flags, blended_cpbl_today, next_step.isoformat())

    # ---- no active shift: check trigger ----
    else:
        if fires:
            msg = start_new_shift(consecutive, d1, run_time_ist, args.dry_run)
        else:
            msg = msg_clean(consecutive)

    print(msg)
    if not args.dry_run and not args.no_post:
        slack_post(msg, dm_only=args.dm_only)
        if not args.date:
            from dashboard_readiness import mark_completed_today
            mark_completed_today('budget_shift_pass', d1)  # after the post succeeds, so a crash mid-run allows retry


if __name__ == '__main__':
    main()
