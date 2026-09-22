# -*- coding: utf-8 -*-
"""One-off (time-boxed) daily check: did Google UAC spend after 10pm IST yesterday?

Nikhil asked (2026-09-22) for a morning check, 11:00 IST daily, on whether the Google UAC
campaign kept spending past 22:00 IST the previous day - a dayparting/ad-schedule concern,
not a budget-shift or CPBL read. Time-boxed through Sunday 2026-09-27 per his own instruction
("put the check till Sunday, 27th") - the script no-ops silently past that date rather than
needing the GH Actions workflow itself deleted on a deadline. Delete this file and
.github/workflows/uac-late-spend-check.yml after 27-Sep once reviewed, same convention as
playstore_ab_check.py.

Matches campaign by 'UAC' substring (case-insensitive), same convention as budget_shift_pass.py's
GOOGLE_IN_SCOPE, so it keeps working if the live campaign gets renamed/recreated - it does NOT
hardcode today's exact campaign name.

"After 10pm" = hour 22 or 23 IST (segments.hour, Google Ads' own account-timezone hour bucket -
confirmed Asia/Kolkata for this account). A small noise floor (LATE_SPEND_FLOOR) avoids flagging
sub-Rs-20 rounding/logging artifacts as a real delivery-past-cutoff event.

Posts to Slack DM only (Nikhil) - always posts something (a clean line if nothing was found),
so a run showing up in the DM confirms the check actually ran rather than silently failing.

Run:  python uac_late_spend_check.py  [--dry-run] [--date YYYY-MM-DD]
Env (GH Actions secrets / local C:\\credentials\\.env):
      GOOGLE_ADS_DEVELOPER_TOKEN, GOOGLE_ADS_CLIENT_ID, GOOGLE_ADS_CLIENT_SECRET,
      GOOGLE_ADS_REFRESH_TOKEN, GOOGLE_ADS_CUSTOMER_ID, SLACK_BOT_TOKEN
"""
import os, json, argparse, datetime, urllib.request

END_DATE = datetime.date(2026, 9, 27)  # last day this check should do anything (Nikhil, 2026-09-22)
LATE_SPEND_FLOOR = 20  # Rs; below this in hours 22-23 combined, treat as rounding noise not real delivery
GOOGLE_CID_DEFAULT = '1218037894'
SLACK_DM_DEFAULT = 'U05A9037VFG'  # Nikhil


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


def get_uac_late_spend(check_date):
    """Returns list of (campaign_name, hour22_spend, hour23_spend, total_spend) for every
    UAC-matching campaign with real spend in hours 22-23 IST on check_date."""
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

    query = f'''
        SELECT segments.hour, campaign.name, metrics.cost_micros
        FROM campaign
        WHERE segments.date = '{check_date.isoformat()}'
    '''
    by_campaign = {}
    for row in ga.search(customer_id=cid, query=query):
        cname = row.campaign.name
        if 'UAC' not in cname.upper():
            continue
        hour = row.segments.hour
        cost = row.metrics.cost_micros / 1_000_000
        d = by_campaign.setdefault(cname, {22: 0.0, 23: 0.0})
        if hour in (22, 23):
            d[hour] += cost

    results = []
    for cname, hrs in by_campaign.items():
        total = hrs[22] + hrs[23]
        if total >= LATE_SPEND_FLOOR:
            results.append((cname, hrs[22], hrs[23], total))
    return results


def fmt_rs(v):
    return f'Rs {v:,.0f}'


def build_message(check_date, results):
    if results:
        lines = [
            f':rotating_light: *UAC late-spend check* - {check_date.isoformat()} spent after 10pm IST',
            '',
        ]
        for cname, h22, h23, total in results:
            lines.append(f'  `{cname}`')
            lines.append(f'    22:00-22:59  {fmt_rs(h22)}')
            lines.append(f'    23:00-23:59  {fmt_rs(h23)}')
            lines.append(f'    Total after 10pm: {fmt_rs(total)}')
        lines += ['', '_One-off check through Sun 27-Sep, per your request._']
    else:
        lines = [
            f':white_check_mark: *UAC late-spend check* - {check_date.isoformat()} clean, no spend after 10pm IST',
            '_One-off check through Sun 27-Sep, per your request._',
        ]
    return '\n'.join(lines)


def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def slack_post_dm(text):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token:
        raise SystemExit('SLACK_BOT_TOKEN not set')
    dm = os.environ.get('SLACK_DM_USER_ID', SLACK_DM_DEFAULT)
    op = slack_api('conversations.open', token, {'users': dm})
    if not op.get('ok'):
        raise SystemExit(f'could not open DM channel: {op.get("error")}')
    resp = slack_api('chat.postMessage', token, {'channel': op['channel']['id'], 'text': text, 'unfurl_links': False, 'mrkdwn': True})
    print('posted to DM:', 'ok' if resp.get('ok') else resp.get('error'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='override the day to check (defaults to yesterday, IST)')
    args = ap.parse_args()
    load_env()

    run_time_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    today_ist = run_time_ist.date()

    if today_ist > END_DATE:
        print(f'{today_ist} is past the {END_DATE} cutoff - one-off check window closed, no-op.')
        return

    check_date = datetime.date.fromisoformat(args.date) if args.date else today_ist - datetime.timedelta(days=1)

    results = get_uac_late_spend(check_date)
    msg = build_message(check_date, results)
    print(msg)
    if not args.dry_run:
        slack_post_dm(msg)


if __name__ == '__main__':
    main()
