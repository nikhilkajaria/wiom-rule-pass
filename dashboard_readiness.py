# -*- coding: utf-8 -*-
"""
Shared readiness gate for rule_pass.py and budget_shift_pass.py.

Neither the growth-portal dashboard nor these scripts have a "D-1 data is
fully loaded" flag to check directly. Per Nikhil, 2026-07-17: dashboard
data has been updating for D-1 at erratic times clustered around 1-1:30 PM
and 3-3:30 PM IST, and it crashed a pass once. So: compare the dashboard's
reported D-1 spend against a DIRECT query to Meta's and Google's own APIs
for that same day (independent ground truth, not derived from the
dashboard) - if the dashboard is significantly under that, the ETL hasn't
finished yet.

Used by both rule_pass.py and budget_shift_pass.py via a 3-attempt retry
schedule (13:30 / 15:30 / 17:30 IST) - see each workflow's .yml for the
attempt-detection logic (keyed off which cron fired, immune to GH Actions
scheduling delay). A lightweight per-script completion marker
(pass_readiness_state.json) makes each attempt idempotent - once a real
run completes successfully for D-1, later attempts that day are a no-op
even if triggered again (e.g. a manual workflow_dispatch).

READINESS_TOLERANCE = 0.90: dashboard spend must be at least 90% of the
directly-queried actual spend to be considered "ready". Some late-arriving
revision is normal even once essentially complete; this just catches the
"still clearly loading" case (seen so far: 20-60% of actual).
"""
import os, json, datetime, urllib.request, urllib.parse

READINESS_TOLERANCE = 0.90
DASH_BASE = 'https://growth-portal.up.railway.app'
META_ACC_DEFAULT = '2007675312900454'
META_VER_DEFAULT = 'v23.0'
GOOGLE_CID_DEFAULT = '1218037894'

_DIR = os.path.dirname(os.path.abspath(__file__))
STATE_PATH = os.path.join(_DIR, 'pass_readiness_state.json')


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


def dget(path):
    req = urllib.request.Request(
        DASH_BASE + path,
        headers={'X-Dashboard-Token': os.environ['WIOM_DASHBOARD_TOKEN'], 'User-Agent': 'wiom-readiness-check'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def get_dashboard_spend_day(d1):
    """
    Dashboard's own reported Meta + Google spend for a single day (war_room).
    NOTE: /api/war_room ignores its own start/end query params and always
    returns the FULL history regardless of what's requested (confirmed
    2026-07-17 - a start=end=single-day query still returned 48 rows back to
    campaign start) - must filter client-side by exact date match, same
    pattern budget_shift_pass.py's get_war_room() already uses. A missing
    date entirely (not just zero spend) is itself a valid "not ready" signal.
    """
    data = dget('/api/war_room?' + urllib.parse.urlencode({'start': d1.isoformat(), 'end': d1.isoformat()}))
    days = data.get('days', data) if isinstance(data, dict) else data
    by_date = {d.get('date'): d for d in days}
    day = by_date.get(d1.isoformat())
    if not day:
        return 0.0
    return float(day.get('meta_spend') or 0) + float(day.get('google_spend') or 0)


def get_actual_meta_spend_day(d1):
    """Direct Meta Insights API: account-wide spend for a single day - independent
    of the dashboard's own ETL, used as ground truth."""
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok:
        return None
    acc = 'act_' + os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT).replace('act_', '')
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    params = {
        'level': 'account',
        'time_range': json.dumps({'since': d1.isoformat(), 'until': d1.isoformat()}),
        'fields': 'spend',
        'access_token': tok,
    }
    url = f'https://graph.facebook.com/{ver}/{acc}/insights?' + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=60) as r:
        resp = json.loads(r.read().decode())
    rows = resp.get('data', [])
    return float(rows[0].get('spend') or 0) if rows else 0.0


def get_actual_google_spend_day(d1):
    """Direct Google Ads API: account-wide spend for a single day - independent
    ground truth, same purpose as the Meta check above."""
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
        query = f'''SELECT metrics.cost_micros FROM customer WHERE segments.date = '{d1.isoformat()}' '''
        total = 0.0
        for row in ga.search(customer_id=cid, query=query):
            total += row.metrics.cost_micros / 1_000_000
        return total
    except Exception as e:
        print(f'warn: actual Google spend check failed - {e}')
        return None


def is_dashboard_data_ready(d1, tolerance=READINESS_TOLERANCE):
    """
    Returns (ready: bool, dashboard_total, actual_total). If either direct
    API check fails outright (not just "zero spend", an actual error), errs
    conservative and returns ready=False rather than risk running on
    incomplete data silently.
    """
    try:
        dash_total = get_dashboard_spend_day(d1)
    except Exception as e:
        print(f'warn: dashboard spend check failed - {e}')
        return False, None, None
    actual_meta = get_actual_meta_spend_day(d1)
    actual_google = get_actual_google_spend_day(d1)
    if actual_meta is None or actual_google is None:
        return False, dash_total, None
    actual_total = actual_meta + actual_google
    if actual_total <= 0:
        return True, dash_total, actual_total  # nothing to compare against - don't block
    ready = (dash_total / actual_total) >= tolerance
    return ready, dash_total, actual_total


# ---- per-script completion tracking (idempotency across retry attempts) ----

def _load_state():
    if os.path.exists(STATE_PATH):
        try:
            with open(STATE_PATH, encoding='utf-8') as f:
                return json.load(f)
        except Exception:
            pass
    return {}


def already_completed_today(script_name, d1):
    state = _load_state()
    return state.get(script_name) == d1.isoformat()


def mark_completed_today(script_name, d1):
    state = _load_state()
    state[script_name] = d1.isoformat()
    with open(STATE_PATH, 'w', encoding='utf-8') as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
