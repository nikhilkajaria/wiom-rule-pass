# -*- coding: utf-8 -*-
"""Budget utilization pass -> Slack (DM only for now). v0.1

  For every active in-scope Meta ad set and Google campaign, compares
  yesterday's actual spend to its daily budget. Flags anything where
  |spend/budget - 1| > 10%, then pulls a first-pass diagnostic:
    - Meta: top creatives within the flagged ad set (spend, CTR, hook/hold rate)
    - Google: network split within the flagged campaign (Search / Search
      Partners / Display / YouTube) via segments.ad_network_type

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
TOP_CREATIVES_N = 5


# ---- spend aggregation (reuses master_export, one call for both channels) ----

def get_day_spend(d1):
    """Returns (meta_by_adset, google_by_campaign, meta_rows_by_adset).
    meta_rows_by_adset keeps the raw creative-level rows for drill-down."""
    rows = dget('/api/master_export?' + f'start={d1.isoformat()}&end={d1.isoformat()}')
    meta_by_adset   = collections.defaultdict(float)
    google_by_camp  = collections.defaultdict(float)
    meta_rows_by_adset = collections.defaultdict(list)
    for r in rows:
        ch = r.get('channel')
        sp = float(r.get('spend') or 0)
        if ch == 'META':
            aset = r.get('ad_set')
            meta_by_adset[aset] += sp
            meta_rows_by_adset[aset].append(r)
        elif ch == 'GOOGLE':
            google_by_camp[r.get('campaign')] += sp
    return meta_by_adset, google_by_camp, meta_rows_by_adset


# ---- Meta drill-down: top creatives in a flagged ad set ----

def top_meta_creatives(rows_for_adset, n=TOP_CREATIVES_N):
    rows = sorted(rows_for_adset, key=lambda r: -(r.get('spend') or 0))
    out = []
    for r in rows[:n]:
        out.append({
            'creative': r.get('creative') or '(Unresolved)',
            'spend': r.get('spend') or 0,
            'impressions': r.get('impressions') or 0,
            'ctr_pct': r.get('ctr_pct'),
            'hook_rate_pct': r.get('hook_rate_pct'),
            'hold_rate_pct': r.get('hold_rate_pct'),
        })
    return out


# ---- Google drill-down: network split for a flagged campaign ----

def google_network_breakdown(campaign_name, d1):
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
        query = f'''
          SELECT segments.ad_network_type, metrics.cost_micros,
                 metrics.impressions, metrics.clicks
          FROM campaign
          WHERE campaign.name = '{safe_name}' AND segments.date = '{d1.isoformat()}'
        '''
        by_net = collections.defaultdict(lambda: {'spend': 0.0, 'impressions': 0, 'clicks': 0})
        for row in ga.search(customer_id=cid, query=query):
            net = row.segments.ad_network_type.name
            by_net[net]['spend']       += row.metrics.cost_micros / 1_000_000
            by_net[net]['impressions'] += row.metrics.impressions
            by_net[net]['clicks']      += row.metrics.clicks
        return dict(by_net)
    except Exception as e:
        print(f'warn: google network breakdown failed for {campaign_name} - {e}')
        return {}


# ---- message formatting ----

def fmt_rs(v): return f'Rs {v:,.0f}'
def fmt_pct(v): return f'{v:.1f}%' if v is not None else 'n/a'


def format_meta_flag(name, budget, spend, util):
    direction = 'UNDER' if util < 1.0 else 'OVER'
    lines = [
        f'  :small_orange_diamond: *Meta* `{name}`  ({direction}-delivery)',
        f'    Budget {fmt_rs(budget)}/day  ->  Spend {fmt_rs(spend)}  ({util*100:.0f}% of budget)',
    ]
    return lines


def format_google_flag(name, budget, spend, util):
    direction = 'UNDER' if util < 1.0 else 'OVER'
    lines = [
        f'  :small_blue_diamond: *Google* `{name}`  ({direction}-delivery)',
        f'    Budget {fmt_rs(budget)}/day  ->  Spend {fmt_rs(spend)}  ({util*100:.0f}% of budget)',
    ]
    return lines


def format_creative_table(creatives):
    lines = []
    for c in creatives:
        lines.append(
            f'      - `{c["creative"][:45]}`  {fmt_rs(c["spend"])}  |  '
            f'imp {c["impressions"]:,}  |  CTR {fmt_pct(c["ctr_pct"])}  |  '
            f'hook {fmt_pct(c["hook_rate_pct"])}  |  hold {fmt_pct(c["hold_rate_pct"])}'
        )
    return lines


def format_network_table(by_net):
    lines = []
    for net, v in sorted(by_net.items(), key=lambda x: -x[1]['spend']):
        lines.append(f'      - {net:<16s}  {fmt_rs(v["spend"])}  |  imp {v["impressions"]:,}  |  clk {v["clicks"]:,}')
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
    meta_spend, google_spend, meta_rows_by_adset = get_day_spend(d1)

    meta_flags   = []
    google_flags = []

    for name, d in meta_budgets.items():
        budget = d['daily_budget']
        spend  = meta_spend.get(name, 0.0)
        if budget <= 0: continue
        util = spend / budget
        if abs(util - 1.0) > UTIL_THRESHOLD:
            meta_flags.append((name, budget, spend, util))

    for name, d in google_budgets.items():
        budget = d['daily_budget']
        spend  = google_spend.get(name, 0.0)
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
            lines += format_meta_flag(name, budget, spend, util)
            creatives = top_meta_creatives(meta_rows_by_adset.get(name, []))
            if creatives:
                lines.append('      _top creatives by spend:_')
                lines += format_creative_table(creatives)
            else:
                lines.append('      _no creative-level rows found for this ad set/day_')
            lines.append('')
        for name, budget, spend, util in sorted(google_flags, key=lambda x: abs(x[3]-1.0), reverse=True):
            lines += format_google_flag(name, budget, spend, util)
            by_net = google_network_breakdown(name, d1)
            if by_net:
                lines.append('      _network split:_')
                lines += format_network_table(by_net)
            else:
                lines.append('      _no network-level rows found for this campaign/day_')
            lines.append('')

    msg = '\n'.join(lines)
    print(msg)
    if not args.dry_run:
        slack_post_dm(msg)


if __name__ == '__main__':
    main()
