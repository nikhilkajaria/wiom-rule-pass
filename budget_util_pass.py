# -*- coding: utf-8 -*-
"""Budget utilization pass -> Slack (DM only for now). v0.4

  For every active in-scope Meta ad set and Google campaign, compares
  yesterday's actual spend to its daily budget. Flags anything where
  |spend/budget - 1| > 10%.

  v0.3: spend is pulled DIRECTLY from Meta Insights / Google Ads APIs, not
  the growth-portal dashboard - it never has today's data (only completed
  days), so it can't get any fresher regardless of when this script runs.

  v0.4: budget is reconstructed as of end-of-day d1, not read live. Live
  budget + a run scheduled close to midnight (00:15 IST) shrinks the edit
  window but doesn't close it, and a --date backtest or a delayed run still
  compares old spend to a possibly-already-edited budget. Tried Google's
  change_event API to reconstruct historical budgets automatically - tested
  2026-07-12 three ways (scoped to the specific campaign, account-wide,
  filtered by CAMPAIGN resource type) and it never surfaced a known,
  confirmed campaign_budget edit; unreliable enough that automating on top
  of it isn't worth it. Manually logged instead: see manual_budget_changes.csv
  (append via log_budget_change.py) - get_budget_as_of() walks that log
  backward from the live value, undoing any edit that happened after
  23:59:59 IST on d1, to reconstruct what was actually in effect that day.

  For each flag, the drill-down answers "what changed" rather than just
  "what's biggest": each creative (Meta) / network (Google) is compared
  against its own trailing LOOKBACK_DAYS-day average spend, and the ones
  that moved the most in the flagged direction are surfaced as the top
  CONTRIBUTORS to the deviation - not just whatever happens to have the
  highest raw spend today (a stable, always-big creative isn't a
  contributor; one that just spiked or dropped is).

  Two different baselines are in play, deliberately:
    - the flag itself: today's spend vs BUDGET (the trigger)
    - the drill-down: today's spend vs each creative/network's own
      trailing average (explains the move; we don't have historical
      budget values to compare against directly)

  Flat +/-10% threshold to begin with (both under- and over-delivery) -
  refine thresholds once we see real output (over-delivery may need a
  looser band since Meta can legitimately spend up to ~2x budget on a
  given day). Same in-scope campaign families as budget_shift_pass.py
  (BFC-VOLUME + RETARGETING for Meta; UAC + DEMANDGEN + SEARCH for
  Google, excluding ToF/AWARENESS) - adjust if "all campaigns" should
  mean something broader.

  Read-only. Never writes to any ad platform.

Run:  python budget_util_pass.py  [--dry-run] [--date YYYY-MM-DD]
Env (Actions secrets / local C:\\credentials\\.env):
      META_ACCESS_TOKEN, GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID,
      GOOGLE_ADS_CLIENT_SECRET, GOOGLE_ADS_REFRESH_TOKEN,
      GOOGLE_ADS_CUSTOMER_ID, SLACK_BOT_TOKEN
"""
import sys, os, csv, json, argparse, datetime, collections, urllib.request, urllib.parse

from budget_shift_pass import (  # also sets sys.stdout to a utf-8 TextIOWrapper
    load_env, slack_api, get_meta_budgets, get_google_budgets,
    META_IN_SCOPE, GOOGLE_IN_SCOPE, GOOGLE_TOF_EXCLUDE,
    META_ACC_DEFAULT, META_VER_DEFAULT, GOOGLE_CID_DEFAULT, SLACK_DM_DEFAULT,
)

UTIL_THRESHOLD  = 0.10   # flag if |spend/budget - 1| > this
LOOKBACK_DAYS   = 6      # trailing baseline window, excluding the flagged day (7 days total)
TOP_CONTRIB_N   = 5

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_DIR = os.path.dirname(os.path.abspath(__file__))
MANUAL_CHANGES_PATH = os.path.join(_DIR, 'manual_budget_changes.csv')


# ---- historical budget reconstruction (manually logged - see module docstring) ----

def load_manual_changes():
    if not os.path.exists(MANUAL_CHANGES_PATH):
        return []
    changes = []
    with open(MANUAL_CHANGES_PATH, newline='', encoding='utf-8') as f:
        for row in csv.DictReader(f):
            changes.append({
                'timestamp': datetime.datetime.fromisoformat(row['timestamp_ist']),
                'platform': row['platform'],
                'entity_name': row['entity_name'],
                'old_budget': float(row['old_budget']),
                'new_budget': float(row['new_budget']),
            })
    return changes


def get_budget_as_of(platform, name, d1, current_budget, changes):
    """
    Reconstructs the budget in effect at 23:59:59 IST on d1, by walking the
    manually-logged change history for this entity backward from now and
    undoing any edit that happened after that moment. Falls back to
    current_budget if no logged changes exist for this entity - the safe
    default when nothing's known to have moved.
    """
    cutoff = datetime.datetime.combine(d1, datetime.time(23, 59, 59), tzinfo=IST)
    relevant = sorted(
        (c for c in changes if c['platform'] == platform and c['entity_name'] == name),
        key=lambda c: c['timestamp'], reverse=True,
    )
    budget = current_budget
    for c in relevant:
        if c['timestamp'] > cutoff:
            budget = c['old_budget']
        else:
            break
    return budget


# ---- Meta: today's spend per ad set + ad-level history for contributor analysis ----

def get_meta_spend_window(d1, lookback=LOOKBACK_DAYS):
    """
    Direct Meta Insights API (level=ad, time_increment=1) for [d1-lookback, d1].
    Returns (today_by_adset, hist).
      today_by_adset[ad_set]              -> spend on d1 (for the flag check)
      hist[ad_set][ad_name][date_iso]     -> spend (for baseline/contributor calc)
    """
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok: return {}, {}
    acc = 'act_' + os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT).replace('act_', '')
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    start = (d1 - datetime.timedelta(days=lookback)).isoformat()
    today_iso = d1.isoformat()

    today_by_adset = collections.defaultdict(float)
    hist = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(float)))

    params = {
        'level': 'ad',
        'time_increment': 1,
        'time_range': json.dumps({'since': start, 'until': today_iso}),
        'fields': 'adset_name,ad_name,spend,date_start',
        'limit': 500,
        'access_token': tok,
    }
    url = f'https://graph.facebook.com/{ver}/{acc}/insights?' + urllib.parse.urlencode(params)
    while url:
        with urllib.request.urlopen(url, timeout=60) as r:
            resp = json.loads(r.read().decode())
        for row in resp.get('data', []):
            aset = row.get('adset_name')
            ad_name = row.get('ad_name') or '(unnamed)'
            date = row.get('date_start')
            sp = float(row.get('spend') or 0)
            hist[aset][ad_name][date] += sp
            if date == today_iso:
                today_by_adset[aset] += sp
        url = resp.get('paging', {}).get('next')
    return dict(today_by_adset), hist


def get_google_day_spend(d1):
    """Direct Google Ads API: campaign-level spend for a single day (d1)."""
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
        query = f'''SELECT campaign.name, metrics.cost_micros
                    FROM campaign WHERE segments.date = '{d1.isoformat()}' '''
        spend = collections.defaultdict(float)
        for row in ga.search(customer_id=cid, query=query):
            spend[row.campaign.name] += row.metrics.cost_micros / 1_000_000
        return dict(spend)
    except Exception as e:
        print(f'warn: google day spend failed - {e}')
        return {}


def meta_creative_contributors(ad_set_hist, d1, direction, n=TOP_CONTRIB_N):
    """direction: 'OVER' or 'UNDER'. Ranks creatives by delta = spend_on_d1 - trailing_avg,
    in the direction matching the flag (grew for OVER, dropped for UNDER)."""
    today_iso = d1.isoformat()
    rows = []
    total_today = 0.0
    total_baseline = 0.0
    for creative, by_date in ad_set_hist.items():
        today = by_date.get(today_iso, 0.0)
        baseline_vals = [v for date, v in by_date.items() if date != today_iso]
        baseline_avg = sum(baseline_vals) / len(baseline_vals) if baseline_vals else 0.0
        delta = today - baseline_avg
        total_today += today
        total_baseline += baseline_avg
        rows.append({
            'creative': creative, 'today': today, 'baseline_avg': baseline_avg,
            'delta': delta, 'baseline_days': len(baseline_vals),
        })
    total_delta = total_today - total_baseline
    if direction == 'OVER':
        movers = sorted([r for r in rows if r['delta'] > 0], key=lambda r: -r['delta'])
    else:
        movers = sorted([r for r in rows if r['delta'] < 0], key=lambda r: r['delta'])
    top = movers[:n]
    explained = sum(r['delta'] for r in top)
    return top, total_delta, explained


# ---- Google: today's network split + history for contributor analysis ----

def get_google_network_window(campaign_name, d1, lookback=LOOKBACK_DAYS):
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
        safe_name = campaign_name.replace("'", "\\'")
        start = (d1 - datetime.timedelta(days=lookback)).isoformat()
        query = f'''
          SELECT segments.ad_network_type, segments.date, metrics.cost_micros
          FROM campaign
          WHERE campaign.name = '{safe_name}'
            AND segments.date BETWEEN '{start}' AND '{d1.isoformat()}'
        '''
        hist = collections.defaultdict(lambda: collections.defaultdict(float))
        for row in ga.search(customer_id=cid, query=query):
            net = row.segments.ad_network_type.name
            date = row.segments.date
            sp = row.metrics.cost_micros / 1_000_000
            hist[net][date] += sp
        return dict(hist)
    except Exception as e:
        print(f'warn: google network window failed for {campaign_name} - {e}')
        return {}


def google_network_contributors(hist, d1, direction, n=TOP_CONTRIB_N):
    today_iso = d1.isoformat()
    rows = []
    total_today = 0.0
    total_baseline = 0.0
    for net, by_date in hist.items():
        today = by_date.get(today_iso, 0.0)
        baseline_vals = [v for date, v in by_date.items() if date != today_iso]
        baseline_avg = sum(baseline_vals) / len(baseline_vals) if baseline_vals else 0.0
        delta = today - baseline_avg
        total_today += today
        total_baseline += baseline_avg
        rows.append({
            'network': net, 'today': today, 'baseline_avg': baseline_avg,
            'delta': delta, 'baseline_days': len(baseline_vals),
        })
    total_delta = total_today - total_baseline
    if direction == 'OVER':
        movers = sorted([r for r in rows if r['delta'] > 0], key=lambda r: -r['delta'])
    else:
        movers = sorted([r for r in rows if r['delta'] < 0], key=lambda r: r['delta'])
    top = movers[:n]
    explained = sum(r['delta'] for r in top)
    return top, total_delta, explained


# ---- message formatting ----

def fmt_rs(v): return f'Rs {v:,.0f}'
def fmt_rs_signed(v): return f'+{fmt_rs(v)}' if v >= 0 else f'-{fmt_rs(abs(v))}'


def format_meta_flag(name, budget, spend, util):
    direction = 'UNDER' if util < 1.0 else 'OVER'
    return [
        f'  :small_orange_diamond: *Meta* `{name}`  ({direction}-delivery)',
        f'    Budget {fmt_rs(budget)}/day  ->  Spend {fmt_rs(spend)}  ({util*100:.0f}% of budget)',
    ]


def format_google_flag(name, budget, spend, util):
    direction = 'UNDER' if util < 1.0 else 'OVER'
    return [
        f'  :small_blue_diamond: *Google* `{name}`  ({direction}-delivery)',
        f'    Budget {fmt_rs(budget)}/day  ->  Spend {fmt_rs(spend)}  ({util*100:.0f}% of budget)',
    ]


def format_contrib_header(total_delta, explained, top_n_found, d1, today_total=0.0):
    day_label = d1.strftime('%b %d')
    if not top_n_found:
        return [f'      _no creative/network moved in this direction vs its trailing {LOOKBACK_DAYS}-day avg - '
                f'deviation may be a recent budget change or account-level factor, not creative-driven_']
    # explained is always same-signed as the movers shown (filtered by flag
    # direction). If the ad-set/campaign-level total barely moved, or moved
    # the OPPOSITE way, these movers are noise cancelled out elsewhere, not
    # the story - this is chronic over/under-delivery, not a fresh swing.
    # An "explains X%" framing would be nonsensical here (sign mismatch can
    # even divide out to a negative percentage) - say so plainly instead.
    flat = today_total > 0 and abs(total_delta) < 0.05 * today_total
    sign_mismatch = total_delta != 0 and ((total_delta > 0) != (explained > 0))
    if flat or sign_mismatch:
        return [f'      _ad-set/campaign-level spend is roughly flat vs its trailing {LOOKBACK_DAYS}-day avg '
                f'(net {fmt_rs_signed(total_delta)}) - {day_label}\'s over/under-budget status looks like an '
                f'ongoing pattern, not a fresh swing. Individual movers in that window:_']
    pct = explained / total_delta * 100
    return [f'      _top contributors to the {fmt_rs_signed(total_delta)} move vs trailing {LOOKBACK_DAYS}-day avg '
            f'(these explain ~{pct:.0f}% of it):_']


def format_creative_contributors(top, d1):
    day_label = d1.strftime('%b %d')
    lines = []
    for c in top:
        lines.append(
            f'      - `{c["creative"][:42]}`  {fmt_rs_signed(c["delta"])}  '
            f'({day_label}: {fmt_rs(c["today"])} vs avg {fmt_rs(c["baseline_avg"])})'
        )
    return lines


def format_network_contributors(top, d1):
    day_label = d1.strftime('%b %d')
    lines = []
    for r in top:
        lines.append(
            f'      - {r["network"]:<16s}  {fmt_rs_signed(r["delta"])}  '
            f'({day_label}: {fmt_rs(r["today"])} vs avg {fmt_rs(r["baseline_avg"])})'
        )
    return lines


# ---- Slack ----

def slack_post_dm(text):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token:
        raise SystemExit('SLACK_BOT_TOKEN not set')
    dm = os.environ.get('SLACK_DM_USER_ID', SLACK_DM_DEFAULT)
    op = slack_api('conversations.open', token, {'users': dm})
    if not op.get('ok'):
        print('warn: could not open DM -', op.get('error'))
        return
    resp = slack_api('chat.postMessage', token,
                      {'channel': op['channel']['id'], 'text': text, 'unfurl_links': False, 'mrkdwn': True})
    print('posted to DM:', 'ok' if resp.get('ok') else resp.get('error'))


# ---- main ----

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='override anchor date YYYY-MM-DD')
    args = ap.parse_args()
    load_env()

    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    meta_budgets   = get_meta_budgets()
    google_budgets = get_google_budgets()
    meta_today_by_adset, meta_hist = get_meta_spend_window(d1)
    manual_changes = load_manual_changes()

    meta_flags   = []
    for name, d in meta_budgets.items():
        budget = get_budget_as_of('meta', name, d1, d['daily_budget'], manual_changes)
        spend  = meta_today_by_adset.get(name, 0.0)
        if budget <= 0: continue
        util = spend / budget
        if abs(util - 1.0) > UTIL_THRESHOLD:
            meta_flags.append((name, budget, spend, util))

    goog_today_by_camp = get_google_day_spend(d1)
    google_flags = []
    for name, d in google_budgets.items():
        budget = get_budget_as_of('google', name, d1, d['daily_budget'], manual_changes)
        spend  = goog_today_by_camp.get(name, 0.0)
        if budget <= 0: continue
        util = spend / budget
        if abs(util - 1.0) > UTIL_THRESHOLD:
            google_flags.append((name, budget, spend, util))

    lines = [f':mag: *Budget Utilization Pass* - {d1.isoformat()}']

    if not meta_flags and not google_flags:
        lines.append(f'  No ad set / campaign outside +/-{UTIL_THRESHOLD*100:.0f}% of budget. Clean.')
    else:
        lines.append(f'  {len(meta_flags)} Meta ad set(s), {len(google_flags)} Google campaign(s) outside +/-{UTIL_THRESHOLD*100:.0f}% of budget:')
        lines.append('')
        for name, budget, spend, util in sorted(meta_flags, key=lambda x: abs(x[3]-1.0), reverse=True):
            direction = 'UNDER' if util < 1.0 else 'OVER'
            lines += format_meta_flag(name, budget, spend, util)
            top, total_delta, explained = meta_creative_contributors(
                meta_hist.get(name, {}), d1, direction)
            lines += format_contrib_header(total_delta, explained, bool(top), d1, today_total=spend)
            lines += format_creative_contributors(top, d1)
            lines.append('')
        for name, budget, spend, util in sorted(google_flags, key=lambda x: abs(x[3]-1.0), reverse=True):
            direction = 'UNDER' if util < 1.0 else 'OVER'
            lines += format_google_flag(name, budget, spend, util)
            hist = get_google_network_window(name, d1)
            top, total_delta, explained = google_network_contributors(hist, d1, direction)
            lines += format_contrib_header(total_delta, explained, bool(top), d1, today_total=spend)
            lines += format_network_contributors(top, d1)
            lines.append('')

    msg = '\n'.join(lines)
    print(msg)
    if not args.dry_run:
        slack_post_dm(msg)


if __name__ == '__main__':
    main()
