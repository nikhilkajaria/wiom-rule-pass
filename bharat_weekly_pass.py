# -*- coding: utf-8 -*-
"""Weekly BHARAT_ALL_SPL review - DM only, advisory, not a kill pass.

Two deliberately different design choices from the PBFC/RMKT kill passes, both argued out in
the wiom-rule-pass session that shipped this (2026-09-05):

1. GEO-LEVEL verdict is benchmarked against Bharat's OWN trailing history, not against C* (the
   Delhi-anchored blended target) and not against a fabricated "Bharat C*". Real data: Bharat's
   blended CPBL over Jul-Aug was Rs2,311 vs a same-period Delhi C* around Rs778-800 - a ~3x gap
   that reflects Bharat's structurally lower serviceability (established earlier this session),
   not a creative-quality problem. Comparing Bharat to Delhi's target would just report "HOLD"
   forever and say nothing useful. Comparing this week's Bharat CPBL to Bharat's OWN trailing
   median answers a more honest question: is Bharat improving, holding, or slipping relative to
   itself.

2. CREATIVE-LEVEL output is an ADVISORY ranking, not a kill list. Real data: of 53 creatives that
   have run in BHARAT_ALL_SPL over 2 months, 34 (64%) never booked a single thing and only 9
   ever reached 10 lifetime bookings - the PBFC-style "judge every creative against a peer
   median, kill the bad ones" machinery would leave most of the pool permanently stuck in
   "insufficient data." Only the ~10-BC-plus subset gets ranked here, against each other's own
   median (not any external target), and only ever as a "worth a look" label - nothing here
   recommends a pause.

Still open, not solved by this script: BHARAT_ALL_SPL's own conversion-rate instability (svc_true
-> BC rate ranged 0%-18.2% across creatives with enough serviceable-lead volume to check, far
noisier than Delhi's) means even this advisory ranking should be read as directional, not final.

Usage: python bharat_weekly_pass.py [--dry-run] [--date YYYY-MM-DD]
"""
import argparse
import collections
import datetime
import statistics
import urllib.parse

import rule_pass as rp

BHARAT_ADSET = "BHARAT_ALL_SPL_L1-L2-L3_BROAD_MULTI_APPSTORE_ABO_BFC-VOLUME_MULTI_NA"
TRAILING_WEEKS = 8              # weeks of history behind this week, for the geo self-benchmark
SCALE_BAND = 0.15               # within +/-15% of trailing median = HOLD; better = SCALE; worse = WATCH


def pull_bharat_rows(d1):
    start = (d1 - datetime.timedelta(weeks=TRAILING_WEEKS + 1)).isoformat()
    rows = rp.dget('/api/master_export?' + urllib.parse.urlencode({'start': start, 'end': d1.isoformat()}))
    return [r for r in rows if r.get('ad_set') == BHARAT_ADSET and r.get('channel') == 'META']


def bharat_active_del():
    """Same shape as rule_pass.meta_active_del() / rmkt_weekly_pass.rmkt_active_del(), scoped
    to BHARAT_ALL_SPL. Unlike those two, this was never wired in when the script was built -
    the creative-level ranking pulled lifetime dashboard attribution with no live Meta
    active-status check, so a paused creative (e.g. JUN26-H-006, confirmed PAUSED 2026-09-08)
    could still show up ranked as if it were a live, actionable option. Fixed 2026-09-08."""
    import json
    import os
    import urllib.request

    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok:
        return None
    acc = os.environ.get('META_AD_ACCOUNT_ID', rp.META_ACC_DEFAULT)
    if not str(acc).startswith('act_'):
        acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', rp.META_VER_DEFAULT)
    active = set()
    calls = 0
    url = f'https://graph.facebook.com/{ver}/{acc}/ads?' + urllib.parse.urlencode(
        {'fields': 'id,name,effective_status,adset{name}', 'limit': 500, 'access_token': tok})
    try:
        while url and calls < 25:
            with urllib.request.urlopen(url, timeout=90) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: Meta active-filter unavailable ->', j['error'].get('message'))
                return None
            for a in j.get('data', []):
                if a.get('effective_status') != 'ACTIVE':
                    continue
                aset = ((a.get('adset') or {}).get('name') or '').upper()
                if 'BHARAT_ALL_SPL' not in aset:
                    continue
                m = rp.CONCEPT_RE.search(a.get('name', '') or '')
                if m:
                    active.add(m.group(0))
            calls += 1
            url = (j.get('paging') or {}).get('next')
        return active
    except Exception as e:
        print('warn: Meta active-filter fetch failed ->', e)
        return None


def week_start(d):
    return d - datetime.timedelta(days=d.weekday())  # Monday


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

    # Same dashboard-readiness gate as rmkt_weekly_pass.py / rule_pass.py's daily run - own
    # script_name so idempotency state never collides with the other passes.
    if not args.dry_run and not args.date:
        from dashboard_readiness import is_dashboard_data_ready, already_completed_today, mark_completed_today
        if already_completed_today('bharat_weekly', d1):
            print(f'already completed for {d1} - skipping (idempotent retry guard)')
            return
        ready, dash_total, actual_total = is_dashboard_data_ready(d1)
        if not ready:
            dash_s = f"Rs{dash_total:,.0f}" if dash_total is not None else 'n/a'
            act_s = f"Rs{actual_total:,.0f}" if actual_total is not None else 'n/a'
            if args.last_retry:
                rp.slack_post(
                    f":rotating_light: *BHARAT_ALL_SPL weekly review* - dashboard data for {d1} still "
                    f"incomplete after 3 attempts (dashboard spend {dash_s} vs actual Meta+Google spend {act_s}). "
                    f"Pass did NOT run today - check the dashboard ETL.",
                    dm_only=True)
                print(f'last retry - data still not ready for {d1} (dashboard={dash_s}, actual={act_s}) - alerted, giving up for today')
            else:
                print(f'dashboard data not ready for {d1} (dashboard={dash_s}, actual={act_s}) - postponing to next retry')
            return

    rows = pull_bharat_rows(d1)
    this_week_start = week_start(d1)

    # --- geo-level: this week's Bharat CPBL vs Bharat's own trailing weekly median ---
    by_week = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0})
    for r in rows:
        rdate = datetime.date.fromisoformat(r['date'])
        wk = week_start(rdate)
        by_week[wk]['spend'] += r.get('spend') or 0
        by_week[wk]['bc'] += r.get('booking_fee_captured') or 0

    this_wk = by_week.get(this_week_start, {'spend': 0.0, 'bc': 0})
    this_cpbl = this_wk['spend'] / this_wk['bc'] if this_wk['bc'] else None

    trailing = [w for wk, w in by_week.items() if wk < this_week_start and w['bc'] > 0]
    trailing_cpbls = [w['spend'] / w['bc'] for w in trailing]
    trailing_median = statistics.median(trailing_cpbls) if trailing_cpbls else None

    if this_cpbl is None or trailing_median is None:
        geo_verdict = 'INSUFFICIENT DATA'
        geo_line = f"this week bc={this_wk['bc']} - not enough to compare"
    else:
        delta = (this_cpbl - trailing_median) / trailing_median
        if delta <= -SCALE_BAND:
            geo_verdict = 'SCALE'
        elif delta >= SCALE_BAND:
            geo_verdict = 'WATCH'
        else:
            geo_verdict = 'HOLD'
        geo_line = (f"this week Rs{this_cpbl:,.0f} vs own trailing {TRAILING_WEEKS}-week median "
                    f"Rs{trailing_median:,.0f} ({delta*100:+.0f}%)")

    # --- creative-level: advisory ranking among creatives with real volume (lifetime, full pull window) ---
    active = bharat_active_del()
    by_cid = collections.defaultdict(lambda: {'spend': 0.0, 'bc': 0})
    for r in rows:
        m = rp.CONCEPT_RE.search(r.get('creative', '') or '')
        if not m:
            continue
        cid = m.group()
        if active is not None and cid not in active:
            continue  # paused/inactive - excluded, not just a low-priority entry
        by_cid[cid]['spend'] += r.get('spend') or 0
        by_cid[cid]['bc'] += r.get('booking_fee_captured') or 0

    # No BC-count floor: a low-booking creative with real spend is exactly the
    # case worth surfacing, not hiding. The only split is BC=0 vs BC>=1 (0
    # bookings has no CPBC to rank by, so those go in their own spend-ranked
    # section instead of being silently dropped).
    with_bc = {cid: v for cid, v in by_cid.items() if v['bc'] > 0}
    zero_bc = {cid: v for cid, v in by_cid.items() if v['bc'] == 0 and v['spend'] > 0}
    cpbls = {cid: v['spend'] / v['bc'] for cid, v in with_bc.items()}
    bharat_median = statistics.median(cpbls.values()) if cpbls else None
    ranked = sorted(cpbls.items(), key=lambda kv: kv[1])
    zero_ranked = sorted(zero_bc.items(), key=lambda kv: kv[1]['spend'], reverse=True)

    total_creatives = len(by_cid)
    end = d1.isoformat()
    lines = [f":compass: *BHARAT_ALL_SPL weekly review* ({end}) - _advisory, no kill recommendations_",
             f"Geo: {geo_verdict} - {geo_line}",
             f"Creative pool: {len(with_bc)}/{total_creatives} active creatives have a booking to rank by CPBC "
             f"(low-BC ones are noisier reads, not hidden - judge by BC alongside CPBC); "
             f"{len(zero_ranked)} active with spend but zero bookings (below)"]
    if active is None:
        lines.append("_Integrity: live Meta active-status check unavailable this run - "
                      "list may include paused creatives._")
    else:
        lines.append("_Integrity: creative active-status vetted live from Meta (effective_status); "
                      "paused excluded._")
    lines.append("")
    if ranked:
        lines.append(f"*All creatives, ranked by CPBC (Bharat-internal median Rs{bharat_median:,.0f})*")
        for cid, cpbl in ranked:
            v = with_bc[cid]
            tag = ':large_green_circle:' if cpbl <= bharat_median * 0.85 else (
                  ':red_circle:' if cpbl >= bharat_median * 1.3 else ':white_circle:')
            lines.append(f"   {tag} `{cid}` {v['bc']} BC, Rs{v['spend']:,.0f}, CPBC Rs{cpbl:,.0f}")
    else:
        lines.append("No creative has a booking yet.")

    if zero_ranked:
        lines.append("")
        lines.append("*Zero bookings, real spend - ranked by spend (no CPBC to judge by)*")
        for cid, v in zero_ranked:
            lines.append(f"   :black_circle: `{cid}` Rs{v['spend']:,.0f}, 0 BC")

    msg = "\n".join(lines)

    if args.dry_run:
        print(msg)
    else:
        rp.slack_post(msg, dm_only=True)  # DM-only, always
        if not args.date:
            from dashboard_readiness import mark_completed_today
            mark_completed_today('bharat_weekly', d1)


if __name__ == '__main__':
    main()
