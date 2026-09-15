# -*- coding: utf-8 -*-
"""Weekly-pass watchdog - DM only. Checks GH Actions run history directly (not each script's
own state file) for whether every Monday-scheduled pass actually fired and succeeded this week.

Built 2026-09-15 (Nikhil, approved via session): weekly cadence has a sharper failure mode than
daily - a missed Monday is a 7-day blind spot, not a couple hours, and the only way anyone found
out about a real account-wide GH Actions scheduling gap (2026-09-14) was a human noticing silence
in the DM. This closes that gap for the weekly passes specifically.

Checks GH Actions run history directly rather than each pass's own idempotency-state file
(bharat_weekly_readiness_state.json etc.) on purpose: weekly-rule-pass.yml (the batched
ISOLATE/geo review, --mode weekly) has no such state file at all - its dashboard-readiness gate
is deliberately skipped for weekly mode, per rule_pass.py main()'s own comment. Run-history is
the one signal all three workflows share, so this is the only uniform check available.

Usage: python weekly_pass_watchdog.py [--dry-run]
"""
import argparse
import datetime
import json
import os
import urllib.parse
import urllib.request

import rule_pass as rp

REPO = 'nikhilkajaria/wiom-rule-pass'
WORKFLOWS = [
    ('weekly-bharat-pass.yml', 'Weekly BHARAT_ALL_SPL review'),
    ('weekly-rmkt-pass.yml', 'Weekly RETARGETING kill+prune'),
    ('weekly-rule-pass.yml', 'Weekly BFC-VOLUME review (ISOLATE/geo)'),
]


def gh_api(path, token):
    req = urllib.request.Request(
        f'https://api.github.com/repos/{REPO}/{path}',
        headers={'Authorization': f'Bearer {token}', 'Accept': 'application/vnd.github+json',
                 'User-Agent': 'weekly-pass-watchdog'})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.loads(r.read().decode())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    args = ap.parse_args()
    rp.load_env()

    token = os.environ.get('GITHUB_TOKEN') or os.environ.get('GH_TOKEN')
    if not token:
        print('warn: no GITHUB_TOKEN available - cannot check run history, skipping')
        return

    now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
    this_monday = (now_ist - datetime.timedelta(days=now_ist.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    since_utc = (this_monday - datetime.timedelta(hours=5, minutes=30)).strftime('%Y-%m-%dT%H:%M:%SZ')

    missing = []
    for wf_file, label in WORKFLOWS:
        try:
            j = gh_api(f'actions/workflows/{wf_file}/runs?' + urllib.parse.urlencode(
                {'created': f'>={since_utc}', 'event': 'schedule', 'per_page': 20}), token)
        except Exception as e:
            missing.append(f"{label} - could not check ({str(e)[:80]})")
            continue
        runs = j.get('workflow_runs', [])
        ok = any(r.get('conclusion') == 'success' for r in runs)
        if not ok:
            statuses = ', '.join(f"{r.get('status')}/{r.get('conclusion')}" for r in runs) or 'no scheduled runs found'
            missing.append(f"{label} - no successful run since Monday ({statuses})")

    if not missing:
        print(f'all {len(WORKFLOWS)} weekly passes have a successful run since {this_monday.date()} - ok')
        return

    msg = (f":rotating_light: *Weekly-pass watchdog* - {this_monday.date()} week: "
           f"{len(missing)}/{len(WORKFLOWS)} weekly pass(es) never posted:\n"
           + "\n".join(f"   - {m}" for m in missing)
           + "\n_Check GH Actions run history directly - this may be the same class of scheduling "
             "gap seen on 2026-09-14, or a real script failure._")
    print(msg)
    if not args.dry_run:
        rp.slack_post(msg, dm_only=True)


if __name__ == '__main__':
    main()
