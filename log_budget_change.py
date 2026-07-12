# -*- coding: utf-8 -*-
"""Log a manual Meta/Google budget change, for budget_util_pass.py's
historical budget reconstruction (get_budget_as_of() in budget_util_pass.py).

Google's change_event API was tested 2026-07-12 (scoped to the specific
campaign, account-wide, filtered by resource type) and never surfaced a
known, confirmed campaign_budget edit - unreliable enough that automating
historical-budget lookup on top of it isn't worth it. Log changes here by
hand (or via this script) instead, right after making them.

--when defaults to now in IST. If logging a change after the fact, pass the
actual edit time explicitly with an IST offset (+05:30) - see
feedback_utc_ist_timestamp_care in memory for why this matters: timestamps
from Slack/other tools are often UTC, not IST, and misreading one shifts the
event to the wrong calendar day.

Run:  python log_budget_change.py --platform google \
          --name "GOOGLE_BA_DEL_SCALE_ABO_UAC_BFC-VOLUME_010626" \
          --old 54000 --new 62000 [--when 2026-07-12T00:47:00+05:30] \
          [--by "Guneet Singh"] [--note "..."]
"""
import argparse, csv, os, datetime

IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
_DIR = os.path.dirname(os.path.abspath(__file__))
LOG_PATH = os.path.join(_DIR, 'manual_budget_changes.csv')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--platform', required=True, choices=['meta', 'google'])
    ap.add_argument('--name', required=True, help='exact ad set (Meta) or campaign (Google) name')
    ap.add_argument('--old', required=True, type=float)
    ap.add_argument('--new', required=True, type=float)
    ap.add_argument('--when', help='ISO8601 with IST offset, e.g. 2026-07-12T00:47:00+05:30 (default: now)')
    ap.add_argument('--by', default='', help='who made the change')
    ap.add_argument('--note', default='')
    args = ap.parse_args()

    when = args.when or datetime.datetime.now(IST).isoformat()

    write_header = not os.path.exists(LOG_PATH) or os.path.getsize(LOG_PATH) == 0
    with open(LOG_PATH, 'a', newline='', encoding='utf-8') as f:
        w = csv.writer(f)
        if write_header:
            w.writerow(['timestamp_ist', 'platform', 'entity_name', 'old_budget', 'new_budget', 'changed_by', 'note'])
        w.writerow([when, args.platform, args.name, f'{args.old:.0f}', f'{args.new:.0f}', args.by, args.note])
    print(f'Logged: {args.platform} `{args.name}`  {args.old:,.0f} -> {args.new:,.0f}  @ {when}')


if __name__ == '__main__':
    main()
