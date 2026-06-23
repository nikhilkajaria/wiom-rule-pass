# -*- coding: utf-8 -*-
"""BFC-VOLUME kill + prune pass -> Slack. Aligned to the Operating Spec (post SD sign-off, 23 Jun).

  DAILY (post-ETL): efficiency kill + zero-BFC kill + cost-velocity brake (KILL-REVIEW) + pool-cap
        prune cut-list. Lifetime CPBFC, lifetime 5-BFC gate, NO calendar grace, active-only median
        (Meta effective_status filter), first-spend anchoring. Decisions on the mature geo (Delhi).
  WEEKLY (review): isolate candidates + geo budget (SCALE/HOLD vs C*) + geo conversion (CAP/CUT).

It NEVER writes to any ad platform - pausing/scaling stays a manual human step. Posts to
#growth-reports and a DM copy.

Run:  python rule_pass.py --mode daily  [--dry-run] [--dm-only] [--date YYYY-MM-DD]
Env (Actions secrets / local C:\\credentials\\.env): WIOM_DASHBOARD_TOKEN, META_ACCESS_TOKEN,
     SLACK_BOT_TOKEN; optional SLACK_CHANNEL_ID, SLACK_DM_USER_ID, META_AD_ACCOUNT_ID, META_API_VERSION.
"""
import sys, io, os, json, re, argparse, datetime, statistics, urllib.request, urllib.parse, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- spec constants (post SD sign-off) ----
CAMPAIGN_START    = '2026-06-01'
WINDOW_DAYS       = 7              # only for prune delivery-velocity + geo/weekly views
CREATIVE_BFC_GATE = 5             # LIFETIME bfc to be efficiency-killable
ZERO_BFC_SPEND    = 10000         # Rs lifetime spend, 0 bfc -> kill
KILL_MULT         = {'L1': 1.0, 'L2': 1.0, 'L3': 1.2, 'untagged': 1.0}
L3_FLIP           = False         # Discovery box not operational -> L3 holds 1.2x (auto-flip to 1.0 later)
BLENDED_TARGET    = 500           # Rs; C* = BLENDED_TARGET * totalBFC / paidBFC
BRAKE_SPEND_FLOOR = 15000         # Rs
BRAKE_CSTAR_MULT  = 5
BRAKE_CPBFC_MULT  = 2.0           # x the creative kill line
ISOLATE_MULT      = 0.7
ISOLATE_BFC_GATE  = 12
POOL_CAP          = 15
MATURE_GEOS       = {'Delhi'}
GEO_BUDGET_BFC_GATE = 10
GEO_CONV_INSTALLS = 100
GEO_CONV_MULT     = 2.0
GEO_RUNAWAY_SPEND = 50000
DASH_BASE         = 'https://growth-portal.up.railway.app'
ADS_MANAGER       = 'https://adsmanager.facebook.com/adsmanager/manage/ads'
CONCEPT_RE        = re.compile(r'JUN26-[TCRH]-\d{3}')
SLACK_CHANNEL_DEFAULT = 'C0B9G0Q68G6'   # #growth-reports
SLACK_DM_DEFAULT      = 'U05A9037VFG'   # Nikhil
META_ACC_DEFAULT      = '2007675312900454'
META_VER_DEFAULT      = 'v23.0'


def load_env():
    path = r'C:\credentials\.env'
    if os.path.exists(path):
        for line in open(path, encoding='utf-8'):
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                k, v = line.split('=', 1); k = k.strip()
                if k not in os.environ: os.environ[k] = v.strip().strip('"').strip("'")


def dget(path):
    req = urllib.request.Request(DASH_BASE + path,
        headers={'X-Dashboard-Token': os.environ['WIOM_DASHBOARD_TOKEN'], 'User-Agent': 'wiom-rule-pass'})
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def geo_of(name):
    n = (name or '').upper()
    if 'DEL' in n or 'DELHI' in n: return 'Delhi'
    if 'BHARAT' in n or 'BHA' in n: return 'Bharat'
    if 'MUM' in n or 'MUMBAI' in n: return 'Mumbai'
    return None


def layer_of(name):
    m = re.search(r'_(L[0-3])_', name or ''); return m.group(1) if m else 'untagged'


def need_of(name):
    parts = (name or '').split('_')
    for i, p in enumerate(parts):
        if re.fullmatch(r'L[0-3]', p) and i + 1 < len(parts): return parts[i + 1]
    return '?'


def meta_active_del():
    """Set of concept_ids with an ACTIVE BOOKNOW ad in a DEL BFC-VOLUME ad set. None if no token."""
    tok = os.environ.get('META_ACCESS_TOKEN')
    if not tok: return None
    acc = os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)
    if not str(acc).startswith('act_'): acc = 'act_' + str(acc)
    ver = os.environ.get('META_API_VERSION', META_VER_DEFAULT)
    active = set(); calls = 0
    url = f'https://graph.facebook.com/{ver}/{acc}/ads?' + urllib.parse.urlencode(
        {'fields': 'name,effective_status,adset{name},campaign{name}', 'limit': 500, 'access_token': tok})
    try:
        while url and calls < 25:
            with urllib.request.urlopen(url, timeout=90) as r:
                j = json.loads(r.read().decode())
            if 'error' in j:
                print('warn: Meta active-filter unavailable ->', j['error'].get('message')); return None
            for a in j.get('data', []):
                if a.get('effective_status') != 'ACTIVE': continue
                nm = a.get('name', '') or ''
                camp = ((a.get('campaign') or {}).get('name') or '').upper()
                aset = ((a.get('adset') or {}).get('name') or '').upper()
                if 'BFC-VOLUME' not in camp or 'BOOKNOW' not in nm.upper() or 'DEL' not in aset: continue
                m = CONCEPT_RE.search(nm)
                if m: active.add(m.group(0))
            calls += 1
            url = (j.get('paging') or {}).get('next')
        return active
    except Exception as e:
        body = ''
        try: body = e.read().decode()[:160]
        except Exception: pass
        # Graceful degrade: one attempt, no aggressive retry (ads-safety). Message flags filter OFF.
        print('warn: Meta active-filter unavailable ->', str(e)[:80], body)
        return None


def compute(d1):
    metric_start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = dget('/api/master_export?' + urllib.parse.urlencode({'start': CAMPAIGN_START, 'end': d1.isoformat()}))
    data = collections.defaultdict(lambda: collections.defaultdict(
        lambda: {'spend': 0.0, 'bfc': 0, 'inst': 0, 'w7s': 0.0, 'w7i': 0, 'layer': 'untagged', 'need': '?'}))
    first = {}
    for r in rows:
        if r.get('channel') != 'META' or 'BFC-VOLUME' not in str(r.get('campaign', '')).upper(): continue
        nm = str(r.get('creative', ''))
        if 'BOOKNOW' not in nm.upper(): continue
        m = CONCEPT_RE.search(nm)
        if not m: continue
        cid = m.group(0); g = geo_of(r.get('ad_set', '')) or geo_of(r.get('campaign', '')) or 'Other'
        dt = str(r.get('date', '')); sp = r.get('spend') or 0; bf = r.get('booking_fee_captured') or 0; ins = r.get('app_installs') or 0
        rec = data[g][cid]
        rec['spend'] += sp; rec['bfc'] += bf; rec['inst'] += ins
        rec['layer'] = layer_of(nm); rec['need'] = need_of(nm)
        if sp > 0 and (cid not in first or dt < first[cid]): first[cid] = dt
        if dt >= metric_start:
            rec['w7s'] += sp; rec['w7i'] += ins
    age = {}
    for cid, ds in first.items():
        try: age[cid] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception: age[cid] = 999
    cstar = None
    try:
        wr = dget('/api/war_room?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        days = wr.get('days', wr) if isinstance(wr, dict) else wr
        tot = sum(d.get('bookings') or 0 for d in days)
        paid = sum((d.get('meta_bfc') or 0) + (d.get('google_bfc') or 0) for d in days)
        if paid: cstar = BLENDED_TARGET * tot / paid
    except Exception: pass
    return data, age, cstar


def cpbfc(rec): return rec['spend'] / rec['bfc'] if rec['bfc'] else float('inf')


def decide(data, age, cstar, active):
    res = {'kills': [], 'reviews': [], 'isolates': [], 'prune_cut': [], 'pool_n': 0, 'continue': 0,
           'monitor': 0, 'median': None, 'brake_spend': None, 'geo_budget': [], 'geo_conv': [],
           'active_filter': active is not None}
    pool = data.get('Delhi', {})
    spent = [c for c in pool if pool[c]['spend'] > 0]
    def act(c): return (active is None) or (c in active)
    elig_active = [c for c in spent if act(c) and pool[c]['bfc'] >= CREATIVE_BFC_GATE]
    med = statistics.median([cpbfc(pool[c]) for c in elig_active]) if elig_active else None
    res['median'] = med
    brake_spend = max(BRAKE_CSTAR_MULT * cstar, BRAKE_SPEND_FLOOR) if cstar else BRAKE_SPEND_FLOOR
    res['brake_spend'] = brake_spend
    verdict = {}
    for c in spent:
        if not act(c): continue
        rec = pool[c]; lb = rec['bfc']; sp = rec['spend']; x = cpbfc(rec); lyr = rec['layer']
        mult = KILL_MULT.get(lyr, 1.0)
        if lyr == 'L3' and L3_FLIP: mult = 1.0
        kt = (mult * med) if med else None
        if lb == 0 and sp >= ZERO_BFC_SPEND:
            verdict[c] = 'KILL'; res['kills'].append((c, lyr, rec['need'], lb, sp, x, 'zero-BFC')); continue
        if kt and sp >= brake_spend and x >= BRAKE_CPBFC_MULT * kt:
            verdict[c] = 'KILL_REVIEW'; res['reviews'].append((c, lyr, rec['need'], lb, sp, x, f'brake (>=2x line, spend Rs{sp:,.0f})')); continue
        if lb >= CREATIVE_BFC_GATE and kt:
            if x >= kt:
                verdict[c] = 'KILL'; res['kills'].append((c, lyr, rec['need'], lb, sp, x, f'efficiency (>= {mult}x median Rs{med:,.0f})')); continue
            if x <= ISOLATE_MULT * med and lb >= ISOLATE_BFC_GATE:
                verdict[c] = 'ISOLATE'; res['isolates'].append((c, lyr, rec['need'], lb, sp, x)); continue
            verdict[c] = 'CONTINUE' if x < med else 'MONITOR'
        else:
            verdict[c] = 'MONITOR'
    res['continue'] = sum(1 for v in verdict.values() if v == 'CONTINUE')
    res['monitor'] = sum(1 for v in verdict.values() if v == 'MONITOR')
    # ---- pool-cap prune (cap 15, layer x need-state coverage, no per-layer floor) ----
    survivors = [c for c in verdict if verdict[c] not in ('KILL', 'KILL_REVIEW')]
    res['pool_n'] = len(survivors)
    if len(survivors) > POOL_CAP:
        keep = set(c for c in survivors if verdict[c] in ('CONTINUE', 'ISOLATE'))
        monitor = [c for c in survivors if verdict[c] == 'MONITOR']
        cells = collections.defaultdict(list)
        for c in monitor: cells[(pool[c]['layer'], pool[c]['need'])].append(c)
        for _cell, mem in cells.items():
            best = sorted(mem, key=lambda c: (-pool[c]['w7s'], cpbfc(pool[c])))[0]; keep.add(best)
        rest = [c for c in monitor if c not in keep]
        ws = [pool[c]['w7s'] for c in rest]
        ineff = [(cpbfc(pool[c]) / med if (pool[c]['bfc'] and med) else 1.0) for c in rest]
        def z(v, arr):
            mu = sum(arr) / len(arr) if arr else 0
            sd = (statistics.pstdev(arr) if len(arr) > 1 else 1) or 1
            return (v - mu) / sd
        score = {c: z(pool[c]['w7s'], ws) - z((cpbfc(pool[c]) / med if (pool[c]['bfc'] and med) else 1.0), ineff) for c in rest}
        for c in sorted(rest, key=lambda c: -score[c]):
            if len(keep) < POOL_CAP: keep.add(c)
        res['prune_cut'] = sorted([c for c in survivors if c not in keep], key=lambda c: pool[c]['w7s'])
    # ---- weekly: geo budget (mature geo, lifetime cpbfc vs C*) + geo conversion ----
    if cstar:
        for g in MATURE_GEOS:
            wc = data.get(g, {})
            gsp = sum(x['spend'] for x in wc.values()); gbfc = sum(x['bfc'] for x in wc.values())
            if gbfc >= GEO_BUDGET_BFC_GATE:
                gcp = gsp / gbfc
                res['geo_budget'].append((g, 'SCALE' if gcp <= cstar else 'HOLD', gcp, gbfc))
    dW = data.get('Delhi', {})
    del_inst = sum(x['inst'] for x in dW.values()); del_bfc = sum(x['bfc'] for x in dW.values())
    del_book = (del_bfc / del_inst) if del_inst else None
    for g, wc in data.items():
        if g in MATURE_GEOS or g == 'Other': continue
        ginst = sum(x['inst'] for x in wc.values()); gbfc = sum(x['bfc'] for x in wc.values()); gsp = sum(x['spend'] for x in wc.values())
        if del_book and ginst >= GEO_CONV_INSTALLS and (gbfc / ginst if ginst else 0) <= (1 / GEO_CONV_MULT) * del_book:
            res['geo_conv'].append((g, f"{ginst} installs, book {100*gbfc/ginst:.2f}% vs Delhi {100*del_book:.2f}%, spend Rs{gsp:,.0f} -> serviceability (CAP/CUT)", gsp))
    return res


def _row(c, lyr, need, lb, sp, x, reason=None):
    xs = f"{x:,.0f}" if x != float('inf') else 'inf'
    base = f"   - `{c}` [{lyr}/{need}] {int(lb)} BFC, Rs{sp:,.0f}, CPBFC Rs{xs}"
    return base + (f" - {reason}" if reason else "")


def ads_link():
    acct = str(os.environ.get('META_AD_ACCOUNT_ID', META_ACC_DEFAULT)).replace('act_', '')
    return f"<{ADS_MANAGER}?act={acct}|Open BFC-VOLUME in Ads Manager>"


def integrity_line(res):
    if res.get('active_filter'):
        return "_Integrity: creative active-status vetted live from Meta (effective_status); paused excluded from the median and the lists._"
    return "_Integrity:_ :warning: _active-status NOT vetted from Meta (unavailable) - basis = dashboard spend only, so recently-paused creatives may still appear. Verify in Ads Manager before acting._"


def msg_daily(res, cstar, end):
    kills, reviews, cut = res['kills'], res['reviews'], res['prune_cut']
    integ = integrity_line(res)
    if not kills and not reviews and not cut:
        return f":white_check_mark: *BFC-VOLUME daily kill+prune* ({end}, DEL BOOKNOW, lifetime): no kills, no brake, no prune. Pool {res['pool_n']}/{POOL_CAP}.\n{integ}"
    medlabel = "active-only median" if res['active_filter'] else "median (incl. paused)"
    if res['median']:
        head = (f":scales: *BFC-VOLUME daily kill + prune* ({end}, DEL BOOKNOW, lifetime)\n"
                f"{medlabel} CPBFC Rs{res['median']:,.0f} | C* Rs{cstar:,.0f} | brake Rs{res['brake_spend']:,.0f} | pool {res['pool_n']}/{POOL_CAP}\n"
                f"_Decisions for review - read-only, pausing is a manual step in Ads Manager._")
    else:
        head = f":scales: *BFC-VOLUME daily kill + prune* ({end})"
    lines = [head, integ, ""]
    if kills:
        lines.append(f"*KILL ({len(kills)})*")
        for k in kills: lines.append(_row(*k))
        lines.append("")
    if reviews:
        lines.append(f"*KILL-REVIEW - cost-velocity brake ({len(reviews)})*  _human look, not auto_")
        for r in reviews: lines.append(_row(*r))
        lines.append("")
    if cut:
        lines.append(f"*PRUNE - pool over cap {POOL_CAP}, cut weakest ({len(cut)})*")
        lines.append("   " + ", ".join(f"`{c}`" for c in cut))
        lines.append("")
    lines.append(f"Held: CONTINUE {res['continue']}, MONITOR {res['monitor']}")
    lines.append(ads_link())
    return "\n".join(lines)


def msg_weekly(res, cstar, start, end):
    isos = res['isolates']
    integ = integrity_line(res)
    if not isos and not res['geo_budget'] and not res['geo_conv']:
        return f":memo: BFC-VOLUME weekly review ({start} to {end}): no isolate/geo actions.\n{integ}"
    lines = [f":memo: *BFC-VOLUME weekly review* ({start} to {end}, DEL BOOKNOW, lifetime)",
             "_Scale/isolate + structural geo layer. Daily handles kills/brake/prune._", integ, ""]
    if isos:
        lines.append(f"*ISOLATE candidates* (<=0.7x median, >=12 BFC -> own ad set) ({len(isos)})")
        for (c, lyr, need, lb, sp, x) in isos: lines.append(_row(c, lyr, need, lb, sp, x, "break into own ad set"))
        lines.append("")
    if res['geo_budget']:
        lines.append(f"*Geo budget* (vs C* ~Rs{cstar:,.0f})" if cstar else "*Geo budget*")
        for (g, a, cp, bf) in sorted(res['geo_budget']): lines.append(f"   - *{g}*: {a} - CPBFC Rs{cp:,.0f}, {int(bf)} BFC")
        lines.append("")
    if res['geo_conv']:
        lines.append("*Geo conversion problems* (serviceability - CAP/CUT)")
        for (g, detail, _s) in res['geo_conv']: lines.append(f"   - *{g}*: {detail}")
        lines.append("")
    lines.append(ads_link())
    return "\n".join(lines)


def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())


def slack_post(text, dm_only=False):
    token = os.environ.get('SLACK_BOT_TOKEN')
    if not token: raise SystemExit("SLACK_BOT_TOKEN not set (use --dry-run to print instead).")
    targets = []
    dm = os.environ.get('SLACK_DM_USER_ID', SLACK_DM_DEFAULT)
    op = slack_api('conversations.open', token, {'users': dm})
    if op.get('ok'): targets.append(('DM', op['channel']['id']))
    else: print("warn: conversations.open failed:", op.get('error'))
    if not dm_only:
        targets.append(('#growth-reports', os.environ.get('SLACK_CHANNEL_ID', SLACK_CHANNEL_DEFAULT)))
    for label, ch in targets:
        resp = slack_api('chat.postMessage', token, {'channel': ch, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
        print(f"posted to {label} ({ch}):", 'ok' if resp.get('ok') else resp.get('error'))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['daily', 'weekly'], default='daily')
    ap.add_argument('--dry-run', action='store_true', help='print the message, do not post')
    ap.add_argument('--dm-only', action='store_true', help='post to the DM copy only (testing), skip the channel')
    ap.add_argument('--date', help='override D-1 anchor YYYY-MM-DD (default = yesterday IST)')
    args = ap.parse_args()
    load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()
    start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat(); end = d1.isoformat()
    data, age, cstar = compute(d1)
    active = meta_active_del()
    res = decide(data, age, cstar, active)
    msg = msg_daily(res, cstar, end) if args.mode == 'daily' else msg_weekly(res, cstar, start, end)
    if args.dry_run: print(msg)
    else: slack_post(msg, dm_only=args.dm_only)


if __name__ == '__main__':
    main()
