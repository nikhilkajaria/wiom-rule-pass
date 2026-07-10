# -*- coding: utf-8 -*-
"""Budget channel rebalancing pass -> Slack. Spec v1.0 (pending SD alignment).

  Runs daily after kill pass (07:30 IST). Checks 7-day rolling Branch-attributed
  CPBL per channel (Meta vs Google). Trigger: gap > 10% for 3 consecutive days.
  Shift: up to 15% of each channel's budget per step, every 3 days, then 7-day
  stabilization. Posts recommendation to Slack; human approves and executes.

  It NEVER writes to any ad platform.

Run:  python budget_shift_pass.py  [--dry-run] [--dm-only] [--date YYYY-MM-DD]
      python budget_shift_pass.py --reset          # clear shift state
Env (Actions secrets / local C:\\credentials\\.env):
      WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN, SLACK_BOT_TOKEN,
      GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
      GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID
"""
import sys, io, os, json, csv, re, argparse, datetime, collections, urllib.request, urllib.parse, tempfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- spec constants (v1.0) ----
TRIGGER_GAP        = 0.10          # channel CPBL gap threshold
TRIGGER_DAYS       = 3             # consecutive days above threshold to fire
MAX_STEP_PCT       = 0.15          # max budget change per step (each channel)
STEP_CADENCE_DAYS  = 3             # days between steps
STABILIZATION_DAYS = 7             # days of read after final step
MIN_BFC_FOR_CPBL   = 20            # min 7-day BFC to trust an ad set's CPBL
MONITORING_THRESH  = 0.20          # flag if metric moves >20% on both DoD and WoW
DASH_BASE          = 'https://growth-portal.up.railway.app'
META_ACC_DEFAULT   = '2007675312900454'
META_VER_DEFAULT   = 'v23.0'
GOOGLE_CID_DEFAULT = '1218037894'
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


def get_campaign_cpbl(d1):
    """7-day Branch-attributed CPBL per campaign and ad set from master_export."""
    start = (d1 - datetime.timedelta(days=6)).isoformat()
    rows = dget('/api/master_export?' + urllib.parse.urlencode({'start': start, 'end': d1.isoformat()}))
    agg = collections.defaultdict(lambda: {'spend': 0.0, 'bfc': 0})
    for r in rows:
        ch   = str(r.get('channel') or '')
        camp = str(r.get('campaign') or '').upper()
        aset = str(r.get('ad_set') or '')
        sp   = float(r.get('spend') or 0)
        bfc  = int(r.get('booking_confirmed') or 0)
        if not sp: continue
        agg[(ch, camp, aset)]['spend'] += sp
        agg[(ch, camp, aset)]['bfc']   += bfc
    return dict(agg)


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


# ---- trigger + shift logic ----

def check_trigger(war_room, d1):
    """
    Returns (fires, gap_today, consecutive_days, meta_cpbl, google_cpbl).
    Uses war_room meta_cpbl / google_cpbl (7-day rolling Branch-attributed).
    """
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
            if gap > TRIGGER_GAP:
                consecutive += 1
            else:
                consecutive = 0
            if i == 0:
                gap_today = gap
                meta_cpbl_today = mc
                google_cpbl_today = gc
    fires = consecutive >= TRIGGER_DAYS
    return fires, gap_today, consecutive, meta_cpbl_today, google_cpbl_today


def compute_shift(gap, meta_total_budget, google_total_budget,
                  from_adset_budget=None, to_camp_budget=None):
    """
    shift_Rs = min(min(gap/2, 15%) x meta_total, 15% x google_total)
    ALSO capped at 15% of the specific ad set / campaign being changed,
    since that's the unit that risks re-entering learning phase.
    """
    step_pct = min(gap / 2, MAX_STEP_PCT)
    term_meta_ch   = step_pct * meta_total_budget
    term_google_ch = MAX_STEP_PCT * google_total_budget
    shift_rs = min(term_meta_ch, term_google_ch)
    binding = 'Google channel' if term_google_ch < term_meta_ch else 'Meta channel'

    if from_adset_budget:
        adset_cap = MAX_STEP_PCT * from_adset_budget
        if adset_cap < shift_rs:
            shift_rs = adset_cap
            binding = 'Meta ad set'
    if to_camp_budget:
        camp_cap = MAX_STEP_PCT * to_camp_budget
        if camp_cap < shift_rs:
            shift_rs = camp_cap
            binding = 'Google campaign'
    return shift_rs, binding


def pick_distribution(shift_rs, meta_budgets, google_budgets, cpbl_data):
    """
    Pick which Meta ad set to reduce from and which Google campaign to add to.
    Meta: prefer DEL_ALL_PBFC (largest reliable lever, cold acquisition).
    Google: prefer UAC if CPBL is best among campaigns with >= MIN_BFC_FOR_CPBL BFC.
    Returns (meta_adset_name, meta_adset_budget, google_camp_name, google_camp_budget).
    """
    # Meta: find DEL_ALL_PBFC in BFC-VOLUME; fall back to largest BFC-VOLUME ad set
    meta_target = None
    meta_target_budget = 0
    for name, d in sorted(meta_budgets.items(), key=lambda x: -x[1]['daily_budget']):
        if d['type'] == 'RETARGETING': continue  # never reduce retargeting
        if 'DEL_ALL_PBFC' in name.upper() or 'DEL' in name.upper():
            if meta_target is None:
                meta_target = name
                meta_target_budget = d['daily_budget']
                break
    if meta_target is None:  # fallback: largest BFC-VOLUME ad set
        for name, d in sorted(meta_budgets.items(), key=lambda x: -x[1]['daily_budget']):
            if d['type'] == 'BFC-VOLUME':
                meta_target = name
                meta_target_budget = d['daily_budget']
                break

    # Google: rank in-scope campaigns by CPBL (best = lowest); require MIN_BFC_FOR_CPBL
    google_scores = []
    for camp_name, d in google_budgets.items():
        camp_up = camp_name.upper()
        # match cpbl_data by campaign name prefix
        for (ch, camp_key, aset), v in cpbl_data.items():
            if ch != 'GOOGLE': continue
            if camp_key and camp_key in camp_up:
                if v['bfc'] >= MIN_BFC_FOR_CPBL:
                    cpbl = v['spend'] / v['bfc']
                    google_scores.append((camp_name, d['daily_budget'], cpbl, v['bfc']))
                break
    google_scores.sort(key=lambda x: x[2])  # sort by CPBL ascending (best first)

    if google_scores:
        google_target, google_target_budget, best_cpbl, _ = google_scores[0]
    else:
        # fallback: UAC by name
        google_target = next((n for n in google_budgets if 'UAC' in n.upper()), None)
        google_target_budget = google_budgets[google_target]['daily_budget'] if google_target else 0

    return meta_target, meta_target_budget, google_target, google_target_budget


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


def _estimate_steps(gap):
    """Rough estimate: each step closes ~12pp of the gap via budget reallocation."""
    if gap >= 0.30: return 4
    if gap >= 0.20: return 3
    if gap >= 0.10: return 2
    return 1


def msg_trigger(gap, consecutive, meta_cpbl, google_cpbl,
                shift_rs, binding,
                meta_target, meta_budget, meta_new,
                google_target, google_budget, google_new,
                step_n=1, anchor_date=None):
    today = anchor_date or datetime.date.today()
    est_steps = _estimate_steps(gap)
    meta_pct = (meta_new - meta_budget) / meta_budget * 100
    google_pct = (google_new - google_budget) / google_budget * 100

    # Build projected step sequence
    step_lines = []
    for s in range(1, est_steps + 1):
        step_date = today + datetime.timedelta(days=(s - step_n) * STEP_CADENCE_DAYS)
        if s == step_n:
            step_lines.append(
                f'  *Step {s} ({step_date.strftime("%b %d")} - execute now):*  '
                f'{fmt_rs(meta_budget)} -> {fmt_rs(meta_new)} Meta  |  '
                f'{fmt_rs(google_budget)} -> {fmt_rs(google_new)} Google  '
                f'({meta_pct:+.1f}% / {google_pct:+.1f}%)'
            )
        else:
            step_lines.append(
                f'  Step {s} ({step_date.strftime("%b %d")} - re-evaluate):  '
                f'~{fmt_rs(shift_rs)}/day more if gap still >10%'
            )
    stab_start = today + datetime.timedelta(days=(est_steps - step_n + 1) * STEP_CADENCE_DAYS)
    stab_end   = stab_start + datetime.timedelta(days=STABILIZATION_DAYS)
    step_lines.append(
        f'  Stabilization ({stab_start.strftime("%b %d")} - {stab_end.strftime("%b %d")}):  7-day read, no new shifts'
    )

    lines = [
        f':arrows_counterclockwise: *Budget Shift Pass* - *TRIGGER FIRES*',
        '',
        f'*Channel CPBL (7-day rolling, Branch-attributed):*',
        f'  Meta: {fmt_rs(meta_cpbl)}  |  Google: {fmt_rs(google_cpbl)}',
        f'  Gap: {gap*100:.1f}%  >10% for {consecutive} consecutive days',
        '',
        f'*Projected shift series  ({est_steps} steps x {fmt_rs(shift_rs)}/day, every 3 days):*',
    ] + step_lines + [
        '',
        f'*Step {step_n} — where to move the money:*',
        f'  Reduce:  `{meta_target}`  {fmt_rs(meta_budget)}/day -> {fmt_rs(meta_new)}/day  ({meta_pct:+.1f}%)',
        f'  Add to:  `{google_target}`  {fmt_rs(google_budget)}/day -> {fmt_rs(google_new)}/day  ({google_pct:+.1f}%)',
        f'  _{binding} side binding at 15% per-unit cap_',
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
            f'  Gap now {gap*100:.1f}% (below 10% threshold) after Step {step_n}.\n'
            f'  Entering 7-day stabilization window. No new shifts until stabilization completes.')


def msg_stabilization(days_remaining, gap):
    return (f':hourglass_flowing_sand: *Budget Shift Pass* - stabilization active.\n'
            f'  {days_remaining} day(s) remaining.  Gap today: {gap*100:.1f}%\n'
            f'  No new shifts until stabilization completes.')


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


# ---- chart ----

def generate_cpbl_chart(war_room, d1):
    """PNG: Meta vs Google CPBL for last 8 days. Returns bytes or None."""
    try:
        import matplotlib
        matplotlib.use('Agg')
        import matplotlib.pyplot as plt
        import matplotlib.dates as mdates

        dates, meta_vals, goog_vals = [], [], []
        for i in range(7, -1, -1):
            d = d1 - datetime.timedelta(days=i)
            day = war_room.get(d.isoformat(), {})
            mc = day.get('meta_cpbl')
            gc = day.get('google_cpbl')
            if mc and gc:
                dates.append(d)
                meta_vals.append(mc)
                goog_vals.append(gc)

        if not dates:
            return None

        fig, ax = plt.subplots(figsize=(8, 3.5))
        ax.plot(dates, meta_vals, 'o-', color='#E8447A', label='Meta CPBL', linewidth=2, markersize=5)
        ax.plot(dates, goog_vals, 'o-', color='#4285F4', label='Google CPBL', linewidth=2, markersize=5)
        ax.fill_between(dates, goog_vals, meta_vals, alpha=0.07, color='#888888')

        # label last point gap
        if len(dates) >= 1:
            gap_pct = (meta_vals[-1] - goog_vals[-1]) / meta_vals[-1] * 100
            ax.annotate(f'Gap {gap_pct:.0f}%', xy=(dates[-1], (meta_vals[-1] + goog_vals[-1]) / 2),
                        xytext=(6, 0), textcoords='offset points', fontsize=8, color='#555555')

        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d %b'))
        ax.xaxis.set_major_locator(mdates.DayLocator())
        plt.xticks(rotation=25, ha='right', fontsize=9)
        ax.yaxis.set_major_formatter(plt.FuncFormatter(lambda x, _: f'Rs {x:,.0f}'))
        ax.set_ylabel('CPBL', fontsize=9)
        ax.set_title('Meta vs Google CPBL — last 8 days', fontsize=10, fontweight='bold', pad=8)
        ax.legend(fontsize=9, loc='upper left')
        ax.grid(axis='y', alpha=0.25, linestyle='--')
        ax.spines['top'].set_visible(False)
        ax.spines['right'].set_visible(False)
        fig.patch.set_facecolor('#FAFAFA')

        buf = io.BytesIO()
        plt.tight_layout()
        plt.savefig(buf, format='png', dpi=130, bbox_inches='tight', facecolor=fig.get_facecolor())
        plt.close(fig)
        buf.seek(0)
        return buf.read()
    except Exception as e:
        print(f'warn: chart generation failed - {e}')
        return None


# ---- Slack ----

def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def slack_upload_png(png_bytes, filename, channel_id, initial_comment, token):
    """Upload PNG via Slack Files v2 API and share to channel_id."""
    # 1. get upload URL — form-encoded (not JSON)
    form1 = urllib.parse.urlencode({'filename': filename, 'length': str(len(png_bytes))}).encode()
    req1 = urllib.request.Request(
        'https://slack.com/api/files.getUploadURLExternal', data=form1,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/x-www-form-urlencoded'})
    with urllib.request.urlopen(req1, timeout=30) as r:
        resp1 = json.loads(r.read())
    if not resp1.get('ok'):
        raise RuntimeError(f'getUploadURLExternal: {resp1.get("error")}')
    upload_url = resp1['upload_url']
    file_id    = resp1['file_id']

    # 2. PUT bytes to upload URL — use requests if available, else urllib with redirect loop
    try:
        import requests as _req
        r2 = _req.put(upload_url, data=png_bytes, headers={'Content-Type': 'image/png'}, timeout=60)
        if r2.status_code not in (200, 204):
            raise RuntimeError(f'PUT upload failed: HTTP {r2.status_code} {r2.text[:200]}')
    except ImportError:
        import http.client, urllib.parse as _up
        url = upload_url
        for _ in range(5):
            p = _up.urlparse(url)
            conn = http.client.HTTPSConnection(p.netloc, timeout=60)
            path = p.path + (f'?{p.query}' if p.query else '')
            conn.request('PUT', path, body=png_bytes, headers={'Content-Type': 'image/png'})
            rr = conn.getresponse()
            if rr.status in (301, 302, 307, 308):
                url = rr.getheader('Location')
                rr.read()
                continue
            if rr.status not in (200, 204):
                raise RuntimeError(f'PUT upload failed: HTTP {rr.status}')
            rr.read()
            break

    # 3. complete: share to channel with initial_comment as the text
    payload3 = json.dumps({
        'files': [{'id': file_id}],
        'channel_id': channel_id,
        'initial_comment': initial_comment,
    }).encode()
    req3 = urllib.request.Request(
        'https://slack.com/api/files.completeUploadExternal', data=payload3,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'})
    with urllib.request.urlopen(req3, timeout=30) as r:
        resp3 = json.loads(r.read())
    if not resp3.get('ok'):
        raise RuntimeError(f'completeUploadExternal: {resp3.get("error")}')
    return resp3


def slack_post(text, dm_only=False, chart_png=None):
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
        if chart_png:
            try:
                slack_upload_png(chart_png, 'cpbl_trend.png', ch, text, token)
                print(f'posted chart to {label}: ok')
                continue
            except Exception as e:
                print(f'warn: chart upload failed for {label} ({e}), falling back to text')
        resp = slack_api('chat.postMessage', token, {'channel': ch, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
        print(f'posted to {label}:', 'ok' if resp.get('ok') else resp.get('error'))


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

    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    state = load_state()
    war_room = get_war_room(d1)
    fires, gap, consecutive, meta_cpbl, google_cpbl = check_trigger(war_room, d1)
    msg = None

    phase = state.get('phase', 'none')

    # ---- stabilization ----
    if phase == 'stabilization':
        stab_end = datetime.date.fromisoformat(state['stabilization_end'])
        days_left = (stab_end - d1).days
        if days_left <= 0:
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
            # Re-evaluate gap at step boundary
            if gap is not None and gap < TRIGGER_GAP:
                # Gap closed - enter stabilization
                stab_end = (d1 + datetime.timedelta(days=STABILIZATION_DAYS)).isoformat()
                state = {'phase': 'stabilization', 'shift': None, 'stabilization_end': stab_end}
                if not args.dry_run:
                    save_state(state)
                msg = msg_gap_closed(gap, step_n)
            elif flags:
                msg = msg_monitoring(step_n, flags, gap or 0, next_step.isoformat())
                # Don't advance step; hold for human review
            else:
                # Fire next step
                meta_budgets   = get_meta_budgets()
                google_budgets = get_google_budgets()
                meta_total   = sum(d['daily_budget'] for d in meta_budgets.values() if d['type'] != 'RETARGETING')
                google_total = sum(d['daily_budget'] for d in google_budgets.values())
                cpbl_data = get_campaign_cpbl(d1)
                meta_target, meta_budget, google_target, google_budget = pick_distribution(
                    None, meta_budgets, google_budgets, cpbl_data)
                shift_rs, binding = compute_shift(gap, meta_total, google_total,
                                                  from_adset_budget=meta_budget,
                                                  to_camp_budget=google_budget)
                meta_new   = meta_budget   - shift_rs
                google_new = google_budget + shift_rs
                next_step_n = step_n + 1
                next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
                state['shift']['step'] = next_step_n
                state['shift']['last_step_date'] = d1.isoformat()
                state['shift']['next_step_date'] = next_step_date
                if not args.dry_run:
                    save_state(state)
                    append_log(d1.isoformat(), gap, shift_rs, 'meta', 'google', next_step_n)
                msg = msg_trigger(gap, consecutive, meta_cpbl, google_cpbl,
                                  shift_rs, binding,
                                  meta_target, meta_budget, meta_new,
                                  google_target, google_budget, google_new,
                                  step_n=next_step_n, anchor_date=d1)
        else:
            # Between steps: monitoring only
            msg = msg_monitoring(step_n, flags, gap or 0, next_step.isoformat())

    # ---- no active shift: check trigger ----
    else:
        if fires and gap is not None:
            meta_budgets   = get_meta_budgets()
            google_budgets = get_google_budgets()
            meta_total   = sum(d['daily_budget'] for d in meta_budgets.values() if d['type'] != 'RETARGETING')
            google_total = sum(d['daily_budget'] for d in google_budgets.values())
            cpbl_data = get_campaign_cpbl(d1)
            meta_target, meta_budget, google_target, google_budget = pick_distribution(
                None, meta_budgets, google_budgets, cpbl_data)
            shift_rs, binding = compute_shift(gap, meta_total, google_total,
                                              from_adset_budget=meta_budget,
                                              to_camp_budget=google_budget)
            meta_new   = meta_budget   - shift_rs
            google_new = google_budget + shift_rs
            next_step_date = (d1 + datetime.timedelta(days=STEP_CADENCE_DAYS)).isoformat()
            state = {
                'phase': 'shift',
                'shift': {
                    'initiated_date':  d1.isoformat(),
                    'step':            1,
                    'shift_rs':        shift_rs,
                    'source_channel':  'meta',
                    'dest_channel':    'google',
                    'meta_adset':      meta_target,
                    'google_campaign': google_target,
                    'trigger_gap_pct': round(gap * 100, 1),
                    'last_step_date':  d1.isoformat(),
                    'next_step_date':  next_step_date,
                },
                'stabilization_end': None,
            }
            if not args.dry_run:
                save_state(state)
                append_log(d1.isoformat(), gap, shift_rs, 'meta', 'google', 1)
            msg = msg_trigger(gap, consecutive, meta_cpbl, google_cpbl,
                              shift_rs, binding,
                              meta_target, meta_budget, meta_new,
                              google_target, google_budget, google_new,
                              step_n=1, anchor_date=d1)
        else:
            msg = msg_clean(gap or 0, consecutive)

    print(msg)
    if not args.dry_run:
        chart_png = generate_cpbl_chart(war_room, d1)
        slack_post(msg, dm_only=args.dm_only, chart_png=chart_png)


if __name__ == '__main__':
    main()
