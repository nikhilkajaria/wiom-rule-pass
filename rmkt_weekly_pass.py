# -*- coding: utf-8 -*-
"""Weekly RETARGETING (DEL_ALL_BFC RMKT) kill+prune review - DM only for now.

Reuses rule_pass.py's decide()/msg_daily() machinery (generic over any data/age/cstar/active
input) scoped to the RETARGETING campaign instead of BFC-VOLUME, since rule_pass.py itself
hardcodes a BFC-VOLUME filter (see meta_active_del() and compute()) and RETARGETING has never
been watched by anything. Confirmed viable on real data (2026-09-05): RMKT has plenty of volume
for this framework (62% of creatives clear CREATIVE_BC_GATE vs PBFC's typical rate, blended CPBL
Rs821 over Jul-Aug, near C*) - see the wiom-rule-pass session that live-previewed this.

Still inherits every open measurement issue from the PBFC side (small-sample noise, no
maturity-adjustment, peer-median-not-C*) - this is a starting shape, not a finished one. Runs
WEEKLY (not daily) and posts DM-only, always, regardless of any future --dm-only flag removal -
this stays DM-only until explicitly promoted to #growth-reports.

Usage: python rmkt_weekly_pass.py [--dry-run] [--date YYYY-MM-DD]
"""
import argparse
import datetime
import sys

import rule_pass as rp

RMKT_ADSET = "DEL_ALL_BFC_L0-L1-L2_RMKT-VV75-MAST90_BOOKNOW_APPSTORE_ABO_RETARGETING_MULTI_NA"


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
        {'fields': 'id,name,effective_status,adset{name},campaign{name}', 'limit': 500, 'access_token': tok})
    try:
        while url and calls < 25:
            with urllib.request.urlopen(url, timeout=90) as r:
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

    first = {}
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            spent_dates = [dt for dt, sp, bf, ins, lyr, need in rws if sp > 0]
            if spent_dates:
                d0 = min(spent_dates)
                if cid not in first or d0 < first[cid]:
                    first[cid] = d0

    window_start = {}
    for cid, d0 in first.items():
        la = last_activation.get(cid)
        window_start[cid] = max(d0, la) if la else d0

    data = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'spend': 0.0, 'bc': 0, 'inst': 0, 'w7s': 0.0, 'w7i': 0, 'layer': 'untagged', 'need': '?'}))
    for g, cmap in raw.items():
        for cid, rws in cmap.items():
            wstart = window_start.get(cid) or first.get(cid, '')
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
    for cid, ds in window_start.items():
        try:
            age[cid] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception:
            age[cid] = 999

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
    ap.add_argument('--dry-run', action='store_true', help='print the message, do not post')
    ap.add_argument('--date', help='override D-1 anchor YYYY-MM-DD (default = yesterday IST)')
    args = ap.parse_args()

    rp.load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()

    end = d1.isoformat()
    active, ad_ids_map = rmkt_active_del()
    last_activation = rp.get_last_activation_dates(d1, active)
    data, age, cstar, funnel_geo = rmkt_compute(d1, last_activation)
    res = rp.decide(data, age, cstar, active, funnel_geo=funnel_geo)

    msg = (rp.msg_daily(res, cstar, end)
           .replace('BFC-VOLUME', 'RETARGETING')
           .replace('daily kill + prune', 'weekly kill + prune')
           .replace('daily cap', 'per-run cap'))

    if args.dry_run:
        print(msg)
    else:
        rp.slack_post(msg, dm_only=True)  # DM-only, always - not promoted to #growth-reports yet


if __name__ == '__main__':
    main()
