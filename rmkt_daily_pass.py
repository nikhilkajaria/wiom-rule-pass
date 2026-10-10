# -*- coding: utf-8 -*-
"""Daily RETARGETING (DEL_ALL_BFC RMKT) kill+prune - #demand-reports + DM, same treatment as the DEL_SCALE pool.

v2 (2026-10-11, Nikhil: "change the RMKT pass to a daily pass just like SCALE"): was rmkt_weekly_pass.py (Mondays).
Now daily with the same 3-attempt readiness gate, the live same-day top-spender deferral, and its OWN kill log /
retro not-acted check / action log (rmkt_kill_pass_log.json, rmkt_kill_action_log.csv - kept apart from the BFC-VOLUME
pool's kill_pass_log.json so the two pools' recos never mix). No Recos-sheet sync (that sheet is the BFC-VOLUME pool's).
History below is the original weekly design note.


Reuses rule_pass.py's decide()/msg_daily() machinery (generic over any data/age/cstar/active
input) scoped to the RETARGETING campaign instead of BFC-VOLUME, since rule_pass.py itself
hardcodes a BFC-VOLUME filter (see meta_active_del() and compute()) and RETARGETING has never
been watched by anything. Confirmed viable on real data (2026-09-05): RMKT has plenty of volume
for this framework (62% of creatives clear CREATIVE_BC_GATE vs PBFC's typical rate, blended CPBL
Rs821 over Jul-Aug, near C*) - see the wiom-rule-pass session that live-previewed this.

Still inherits every open measurement issue from the PBFC side (small-sample noise, no
maturity-adjustment, peer-median-not-C*) - this is a starting shape, not a finished one. Runs
WEEKLY (not daily) and posts DM-only, always, regardless of any future --dm-only flag removal -
this stays DM-only until explicitly promoted to #demand-reports.

Usage: python rmkt_daily_pass.py [--dry-run] [--date YYYY-MM-DD] [--last-retry]
"""
import argparse
import datetime
import sys

import rule_pass as rp
import os
import urllib.request

RMKT_ADSET = "DEL_ALL_BFC_L0-L1-L2_RMKT-VV75-MAST90_BOOKNOW_APPSTORE_ABO_RETARGETING_MULTI_NA"
RMKT_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rmkt_kill_pass_log.json')
RMKT_ACTION_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'rmkt_kill_action_log.csv')



def _meta_req(url):
    """Meta Graph request with the token in the Authorization header, never in the URL or its paging links
    (rules/ads-api-safety.md; 2026-10-10)."""
    return urllib.request.Request(url, headers={'Authorization': 'Bearer ' + os.environ.get('META_ACCESS_TOKEN', '')})

def rmkt_active_del():
    """Same shape as rule_pass.meta_active_del(), scoped to RETARGETING instead of BFC-VOLUME."""
    import collections
    import json
    import os
    import urllib.parse
    import urllib.request

    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok:
        return None, {}
    acc = os.environ.get('META_AD_ACCOUNT_ID', rp.META_ACC_DEFAULT)
    if not str(acc).startswith('act_'):
        acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    active = set()
    ad_ids_map = collections.defaultdict(list)
    calls = 0
    url = f'https://graph.facebook.com/{ver}/{acc}/ads?' + urllib.parse.urlencode(
        {'fields': 'id,name,effective_status,adset{name},campaign{name}', 'limit': 500})
    try:
        while url and calls < 25:
            with urllib.request.urlopen(_meta_req(url), timeout=90) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: Meta active-filter unavailable ->', j['error'].get('message'))
                return None, {}
            for a in j.get('data', []):
                if a.get('effective_status') != 'ACTIVE':
                    continue
                nm = a.get('name', '') or ''
                camp = ((a.get('campaign') or {}).get('name') or '').upper()
                aset = ((a.get('adset') or {}).get('name') or '').upper()
                if 'RETARGETING' not in camp or 'BOOKNOW' not in nm.upper() or 'DEL' not in aset:
                    continue
                m = rp.CONCEPT_RE.search(nm)
                if m:
                    cid = m.group(0)
                    active.add(cid)
                    if a.get('id'):
                        ad_ids_map[cid].append(a['id'])
            calls += 1
            url = (j.get('paging') or {}).get('next')
        return active, dict(ad_ids_map)
    except Exception as e:
        print('warn: Meta active-filter unavailable ->', str(e)[:120])
        return None, {}


def rmkt_compute(d1, last_activation=None):
    """Same shape as rule_pass.compute(), scoped to RETARGETING instead of BFC-VOLUME."""
    import collections
    import urllib.parse

    last_activation = last_activation or {}
    metric_start = (d1 - datetime.timedelta(days=rp.WINDOW_DAYS - 1)).isoformat()
    rows = rp.dget('/api/master_export?' + urllib.parse.urlencode({'start': rp.CAMPAIGN_START, 'end': d1.isoformat()}))

    raw = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        if r.get('channel') != 'META' or 'RETARGETING' not in str(r.get('campaign', '')).upper():
            continue
        nm = str(r.get('creative', ''))
        if 'BOOKNOW' not in nm.upper():
            continue
        m = rp.CONCEPT_RE.search(nm)
        if not m:
            continue
        cid = m.group(0)
        g = rp.geo_of(r.get('ad_set', '')) or rp.geo_of(r.get('campaign', '')) or 'Other'
        dt = str(r.get('date', ''))
        sp = r.get('spend') or 0
        bf = r.get('booking_confirmed') or 0
        ins = r.get('app_installs') or 0
        raw[g][cid].append((dt, sp, bf, ins, rp.layer_of(nm), rp.need_of(nm)))

    # v2.12 (2026-09-15): keyed (geo, cid), not cid alone - see rule_pass.compute()'s matching
    # comment (AUG26-T-120/T-121 incident) for why a cid-global first[]/window_start[] falsely
    # ages out a concept that's brand-new in THIS pool but has older history in another.
    first = {}
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            spent_dates = [dt for dt, sp, bf, ins, lyr, need in rws if sp > 0]
            if spent_dates:
                first[(g, cid)] = min(spent_dates)

    window_start = {}
    for (g, cid), d0 in first.items():
        la = last_activation.get(cid)
        window_start[(g, cid)] = max(d0, la) if la else d0

    data = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'spend': 0.0, 'bc': 0, 'inst': 0, 'w7s': 0.0, 'w7i': 0, 'layer': 'untagged', 'need': '?'}))
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            wstart = window_start.get((g, cid)) or first.get((g, cid), '')
            rec = data[g][cid]
            for dt, sp, bf, ins, lyr, need in rws:
                rec['layer'] = lyr
                rec['need'] = need
                if dt >= wstart:
                    rec['spend'] += sp
                    rec['bc'] += bf
                    rec['inst'] += ins
                if dt >= metric_start:
                    rec['w7s'] += sp
                    rec['w7i'] += ins
                    rec['w7b'] = rec.get('w7b', 0) + bf

    age = {}
    for (g, cid), ds in window_start.items():
        try:
            age[(g, cid)] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception:
            age[(g, cid)] = 999

    cstar = None
    try:
        wr = rp.dget('/api/war_room?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        days = wr.get('days', wr) if isinstance(wr, dict) else wr
        tot = sum(d.get('bookings') or 0 for d in days)
        paid = sum((d.get('meta_bfc') or 0) + (d.get('google_bfc') or 0) for d in days)
        if paid:
            cstar = rp.BLENDED_TARGET * tot / paid
    except Exception:
        pass

    return data, age, cstar, {}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true', help='print the message, do not post or write state')
    ap.add_argument('--date', help='override D-1 anchor YYYY-MM-DD (default = yesterday IST)')
    ap.add_argument('--last-retry', action='store_true',
                     help='final scheduled attempt of the day - alert (DM) if dashboard data is still '
                          'not ready, instead of quietly postponing to the next retry')
    args = ap.parse_args()

    rp.load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    # Same dashboard-readiness gate as rule_pass.py's daily run (2026-07-17) - only for a real,
    # unattended scheduled run, not a --dry-run preview or a --date backtest. Separate
    # script_name ('rmkt_daily') so this pass's idempotent-completion tracking never collides
    # with the daily BFC-VOLUME pass's own state.
    if not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today, mark_completed_today
        if already_completed_today('rmkt_daily', d1):
            print(f'already completed for {d1} - skipping (idempotent retry guard)')
            return
        ready, dash_total, actual_total = is_dashboard_data_ready(d1)
        if not ready:
            dash_s = f"Rs{dash_total:,.0f}" if dash_total is not None else 'n/a'
            act_s = f"Rs{actual_total:,.0f}" if actual_total is not None else 'n/a'
            if args.last_retry:
                rp.slack_post(
                    f":rotating_light: *DEL RMKT BOOKNOW - RETARGETING daily kill+prune* - dashboard data for {d1} still "
                    f"incomplete after 3 attempts (dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). "
                    f"Pass did NOT run today - check the dashboard ETL.",
                    dm_only=True)
                print(f'last retry - data still not ready for {d1} (dashboard={dash_s}, actual={act_s}) - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    end = d1.isoformat()
    active, ad_ids_map = rmkt_active_del()
    last_activation = rp.get_last_activation_dates(d1, active)
    data, age, cstar, funnel_geo = rmkt_compute(d1, last_activation)
    res = rp.decide(data, age, cstar, active, funnel_geo=funnel_geo)

    # Live same-day top-spender deferral - same as rule_pass.py main(): a KILL on one of today's top-2 live spenders
    # moves to MONITOR (shown under PROTECTED), so nobody pauses what is becoming the ad set's biggest spender right now.
    res['deferred_live_spend'] = []
    if not args.date:
        today_spend = rp.meta_today_spend(ad_ids_map, d1 + datetime.timedelta(days=1))
        top_today = {c for c in sorted(today_spend, key=lambda c: -today_spend[c])[:2] if today_spend[c] > 0} if today_spend else set()
        if top_today:
            keep = []
            for k in res['kills']:
                if k[0] in top_today:
                    res['deferred_live_spend'].append(k); res['verdict'][k[0]] = 'MONITOR'; res['monitor'] += 1
                else:
                    keep.append(k)
            res['kills'] = keep

    # Own kill log + retro 'not acted' check (rule_pass's log helpers read the module-level paths at call time).
    rp.LOG_PATH, rp.ACTION_LOG_PATH = RMKT_LOG_PATH, RMKT_ACTION_LOG_PATH
    still_flagged_today = ({k[0] for k in res.get('kills', [])} | {k[0] for k in res.get('deferred_kills', [])} |
                           {t[0] for t in res.get('deferred_top_spender', [])} | {t[0] for t in res.get('deferred_live_spend', [])})
    log = rp.load_log()
    unacted = rp.retro_check(log, d1, still_flagged_today=still_flagged_today)
    if not args.dry_run:
        rp.write_action_log_csv(log, d1)
        rp.save_log(rp.write_log_entry(log, res, d1, ad_ids_map, res['median']))

    msg = (rp.msg_daily(res, cstar, end, unacted=unacted, label='DEL RMKT BOOKNOW')
           .replace('BFC-VOLUME', 'RETARGETING'))

    if args.dry_run:
        print(msg)
    else:
        rp.slack_post(msg, dm_only=False)  # #demand-reports + DM (promoted 2026-09-15, Nikhil)
        if not args.date:
            from dashboard_readiness import mark_completed_today
            mark_completed_today('rmkt_daily', d1)  # after the post succeeds, so a crash mid-run allows retry

if __name__ == '__main__':
    main()
