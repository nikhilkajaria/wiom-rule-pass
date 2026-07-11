# -*- coding: utf-8 -*-
"""Budget utilization pass -> Slack (DM only for now). v0.2

  For every active in-scope Meta ad set and Google campaign, compares
  yesterday's actual spend to its daily budget. Flags anything where
  |spend/budget - 1| > 10%.

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
      WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN,
      GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
      GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID, SLACK_BOT_TOKEN
"""
import sys, os, argparse, datetime, collections

from budget_shift_pass import (  # also sets sys.stdout to a utf-8 TextIOWrapper
    load_env, dget, slack_api, get_meta_budgets, get_google_budgets,
    META_IN_SCOPE, GOOGLE_IN_SCOPE, GOOGLE_TOF_EXCLUDE,
    GOOGLE_CID_DEFAULT, SLACK_DM_DEFAULT,
)

UTIL_THRESHOLD  = 0.10   # flag if |spend/budget - 1| > this
LOOKBACK_DAYS   = 6      # trailing baseline window, excluding the flagged day (7 days total)
TOP_CONTRIB_N   = 5


# ---- Meta: today's spend per ad set + creative-level history for contributor analysis ----

def get_meta_window(d1, lookback=LOOKBACK_DAYS):
    """
    Returns (today_by_adset, hist, today_detail).
      today_by_adset[ad_set]                    -> spend on d1 (for the flag check)
      hist[ad_set][creative][date_iso]           -> spend (for baseline/contributor calc)
      today_detail[(ad_set, creative)]           -> {impressions, ctr_pct, hook_rate_pct, hold_rate_pct} on d1
    """
    start = (d1 - datetime.timedelta(days=lookback)).isoformat()
    rows = dget('/api/master_export?' + f'start={start}&end={d1.isoformat()}')
    today_iso = d1.isoformat()
    today_by_adset = collections.defaultdict(float)
    hist = collections.defaultdict(lambda: collections.defaultdict(lambda: collections.defaultdict(float)))
    today_detail = {}
    for r in rows:
        if r.get('channel') != 'META': continue
        aset = r.get('ad_set')
        creative = r.get('creative') or '(Unresolved)'
        date = r.get('date')
        sp = float(r.get('spend') or 0)
        hist[aset][creative][date] += sp
        if date == today_iso:
            today_by_adset[aset] += sp
            today_detail[(aset, creative)] = {
                'impressions': r.get('impressions') or 0,
                'ctr_pct': r.get('ctr_pct'),
                'hook_rate_pct': r.get('hook_rate_pct'),
                'hold_rate_pct': r.get('hold_rate_pct'),
            }
    return today_by_adset, hist, today_detail


def meta_creative_contributors(ad_set_hist, today_detail, ad_set, d1, direction, n=TOP_CONTRIB_N):
    """direction: 'OVER' or 'UNDER'. Ranks creatives by delta = today - trailing_avg,
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
            **today_detail.get((ad_set, creative), {}),
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
          SELECT segments.ad_network_type, segments.date, metrics.cost_micros,
                 metrics.impressions, metrics.clicks
          FROM campaign
          WHERE campaign.name = '{safe_name}'
            AND segments.date BETWEEN '{start}' AND '{d1.isoformat()}'
        '''
        hist = collections.defaultdict(lambda: collections.defaultdict(float))
        today_detail = collections.defaultdict(lambda: {'impressions': 0, 'clicks': 0})
        today_iso = d1.isoformat()
        for row in ga.search(customer_id=cid, query=query):
            net = row.segments.ad_network_type.name
            date = row.segments.date
            sp = row.metrics.cost_micros / 1_000_000
            hist[net][date] += sp
            if date == today_iso:
                today_detail[net]['impressions'] += row.metrics.impressions
                today_detail[net]['clicks']      += row.metrics.clicks
        return dict(hist), dict(today_detail)
    except Exception as e:
        print(f'warn: google network window failed for {campaign_name} - {e}')
        return {}, {}


def google_network_contributors(hist, today_detail, d1, direction, n=TOP_CONTRIB_N):
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
            **today_detail.get(net, {'impressions': 0, 'clicks': 0}),
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
def fmt_pct(v): return f'{v:.1f}%' if v is not None else 'n/a'


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


def format_contrib_header(total_delta, explained, top_n_found, today_total=0.0):
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
                f'(net {fmt_rs_signed(total_delta)}) - today\'s over/under-budget status looks like an ongoing '
                f'pattern, not a fresh swing. Individual movers in that window:_']
    pct = explained / total_delta * 100
    return [f'      _top contributors to the {fmt_rs_signed(total_delta)} move vs trailing {LOOKBACK_DAYS}-day avg '
            f'(these explain ~{pct:.0f}% of it):_']


def format_creative_contributors(top):
    lines = []
    for c in top:
        lines.append(
            f'      - `{c["creative"][:42]}`  {fmt_rs_signed(c["delta"])}  '
            f'(today {fmt_rs(c["today"])} vs avg {fmt_rs(c["baseline_avg"])})  |  '
            f'CTR {fmt_pct(c.get("ctr_pct"))}  |  hook {fmt_pct(c.get("hook_rate_pct"))}  |  hold {fmt_pct(c.get("hold_rate_pct"))}'
        )
    return lines


def format_network_contributors(top):
    lines = []
    for r in top:
        lines.append(
            f'      - {r["network"]:<16s}  {fmt_rs_signed(r["delta"])}  '
            f'(today {fmt_rs(r["today"])} vs avg {fmt_rs(r["baseline_avg"])})  |  '
            f'imp {r.get("impressions", 0):,}  |  clk {r.get("clicks", 0):,}'
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
    meta_today_by_adset, meta_hist, meta_today_detail = get_meta_window(d1)

    meta_flags   = []
    for name, d in meta_budgets.items():
        budget = d['daily_budget']
        spend  = meta_today_by_adset.get(name, 0.0)
        if budget <= 0: continue
        util = spend / budget
        if abs(util - 1.0) > UTIL_THRESHOLD:
            meta_flags.append((name, budget, spend, util))

    # today's Google spend from a single-day master_export pull (kept separate
    # from Meta's window fetch since Google's drill-down uses the Ads API
    # directly, not master_export history)
    goog_today_rows = dget('/api/master_export?' + f'start={d1.isoformat()}&end={d1.isoformat()}')
    goog_today_by_camp = collections.defaultdict(float)
    for r in goog_today_rows:
        if r.get('channel') == 'GOOGLE':
            goog_today_by_camp[r.get('campaign')] += float(r.get('spend') or 0)
    google_flags = []
    for name, d in google_budgets.items():
        budget = d['daily_budget']
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
                meta_hist.get(name, {}), meta_today_detail, name, d1, direction)
            lines += format_contrib_header(total_delta, explained, bool(top), today_total=spend)
            lines += format_creative_contributors(top)
            lines.append('')
        for name, budget, spend, util in sorted(google_flags, key=lambda x: abs(x[3]-1.0), reverse=True):
            direction = 'UNDER' if util < 1.0 else 'OVER'
            lines += format_google_flag(name, budget, spend, util)
            hist, today_detail = get_google_network_window(name, d1)
            top, total_delta, explained = google_network_contributors(hist, today_detail, d1, direction)
            lines += format_contrib_header(total_delta, explained, bool(top), today_total=spend)
            lines += format_network_contributors(top)
            lines.append('')

    msg = '\n'.join(lines)
    print(msg)
    if not args.dry_run:
        slack_post_dm(msg)


if __name__ == '__main__':
    main()
