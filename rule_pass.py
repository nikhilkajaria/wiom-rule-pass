# -*- coding: utf-8 -*-
"""Rule-pass for BFC-VOLUME creatives - aligned to Creative Performance Framework v2, with the
framework's DAILY vs WEEKLY cadence honoured.

  DAILY  (every morning, post-ETL): the daily DECISION layer (per the SD-reply doc - the latest).
         Posts the in-pool creative verdicts - kill / iterate / isolate - run straight off the
         framework, for one-click approval THAT DAY. Plus circuit-breaker alerts (runaway zero-BFC
         spend, cold-start geo runaway). "Kills/pauses/continues I run daily off the framework."

  WEEKLY (the review with SD): the STRUCTURAL / geo layer - geo budget (SCALE / HOLD / CUT vs C*),
         geo conversion problems, and the slower questions (pool variety, pipeline). The bigger
         structural calls, not routine creative pruning. ("Structural geo calls = the review meeting.")

  (v3 supersedes the dated v2 cadence table, which put L1 creative on a weekly batch; per the SD doc,
   creative verdicts are a daily decision and only structural/geo waits for the weekly review.)

It NEVER writes to any ad platform - pausing/scaling stays a manual human step.

Faithful to v2: BOOKNOW-only; rolling 7-day CPBFC; lifetime BOOKNOW spend for zero-BFC; decisions on
the MATURE geo (Delhi) with cross-geo pooling; >=7-day eligibility; efficiency needs >=5 BFC.

Run:  python rule_pass.py --mode daily   [--dry-run] [--date YYYY-MM-DD]
      python rule_pass.py --mode weekly  [--dry-run] [--date YYYY-MM-DD]
Env (Actions secrets / local C:\\credentials\\.env): WIOM_DASHBOARD_TOKEN, SLACK_BOT_TOKEN, SLACK_DM_USER_ID.
"""
import sys, io, os, json, re, argparse, datetime, statistics, urllib.request, urllib.parse, collections
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

# ---- v2 constants ----
CAMPAIGN_START    = '2026-06-01'
WINDOW_DAYS       = 7
ELIG_AGE_DAYS     = 7
CREATIVE_BFC_GATE = 5
P2_LIFETIME_SPEND = 10000
JUNK_MULT         = 2.0
MIN_INSTALLS_JUNK = 50
KILL_MULT         = {'L1': 1.0, 'L2': 1.0, 'L3': 1.2, 'untagged': 1.0}
ISOLATE_MULT      = 0.7
ISOLATE_BFC_GATE  = 12
MATURE_GEOS       = {'Delhi'}
GEO_BUDGET_BFC_GATE = 10          # min geo 7d BFC to make a budget call
BLENDED_TARGET    = 500           # Rs; C* = BLENDED_TARGET * totalBFC / paidBFC
GEO_RUNAWAY_SPEND = 50000         # Rs/week: cold-start geo burning >= this while failing conversion = daily runaway
GEO_CONV_INSTALLS = 100
GEO_CONV_MULT     = 2.0
DASH_BASE         = 'https://growth-portal.up.railway.app'
ADS_MANAGER       = 'https://adsmanager.facebook.com/adsmanager/manage/ads'
CONCEPT_RE        = re.compile(r'JUN26-[TCRH]-\d{3}')

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

def compute(d1):
    metric_start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat()
    rows = dget('/api/master_export?' + urllib.parse.urlencode({'start': CAMPAIGN_START, 'end': d1.isoformat()}))
    life = collections.defaultdict(lambda: collections.defaultdict(lambda: {'spend': 0.0, 'bfc': 0}))
    win  = collections.defaultdict(lambda: collections.defaultdict(lambda: {'spend': 0.0, 'bfc': 0, 'inst': 0, 'impr': 0, 'layer': 'untagged'}))
    first = {}
    for r in rows:
        if r.get('channel') != 'META' or 'BFC-VOLUME' not in str(r.get('campaign', '')).upper(): continue
        nm = str(r.get('creative', ''))
        if 'BOOKNOW' not in nm.upper(): continue
        m = CONCEPT_RE.search(nm)
        if not m: continue
        cid = m.group(0); g = geo_of(r.get('ad_set', '')) or geo_of(r.get('campaign', '')) or 'Other'
        dt = str(r.get('date', '')); spend = r.get('spend') or 0; bfc = r.get('booking_fee_captured') or 0
        if spend > 0 and (cid not in first or dt < first[cid]): first[cid] = dt
        life[g][cid]['spend'] += spend; life[g][cid]['bfc'] += bfc
        if dt >= metric_start:
            w = win[g][cid]; w['spend'] += spend; w['bfc'] += bfc
            w['inst'] += r.get('app_installs') or 0; w['impr'] += r.get('impressions') or 0; w['layer'] = layer_of(nm)
    age = {}
    for cid, ds in first.items():
        try: age[cid] = (d1 - datetime.date.fromisoformat(ds)).days
        except Exception: age[cid] = 999
    # C* from war_room (blended target * total BFC / paid BFC) over the window
    cstar = None
    try:
        wr = dget('/api/war_room?' + urllib.parse.urlencode({'start': metric_start, 'end': d1.isoformat()}))
        days = wr.get('days', wr) if isinstance(wr, dict) else wr
        tot = sum(d.get('bookings') or 0 for d in days)
        paid = sum((d.get('meta_bfc') or 0) + (d.get('google_bfc') or 0) for d in days)
        if paid: cstar = BLENDED_TARGET * tot / paid
    except Exception: pass
    return life, win, age, cstar

def decide(life, win, age, cstar):
    res = {'kills': [], 'isolates': [], 'geo_budget': [], 'geo_conv': [], 'median': None}
    for G in MATURE_GEOS:
        w = win.get(G, {}); lf = life.get(G, {})
        elig = [c for c in w if age.get(c, 999) >= ELIG_AGE_DAYS]
        cpbfcs = [w[c]['spend'] / w[c]['bfc'] for c in elig if w[c]['bfc'] >= CREATIVE_BFC_GATE]
        med = statistics.median(cpbfcs) if cpbfcs else None; res['median'] = med
        books = [w[c]['bfc'] / w[c]['inst'] for c in elig if w[c]['inst'] >= MIN_INSTALLS_JUNK]
        med_book = statistics.median(books) if books else None
        irs = [w[c]['inst'] / w[c]['impr'] for c in elig if w[c]['impr'] > 0]
        med_ir = statistics.median(irs) if irs else None
        for cid in sorted(elig):
            wc = w[cid]; layer = wc['layer']; a = age.get(cid, 999)
            lifb = lf.get(cid, {}).get('bfc', 0); lifs = lf.get(cid, {}).get('spend', 0)
            sp, bf, inst, impr = wc['spend'], wc['bfc'], wc['inst'], wc['impr']
            if lifb == 0 and lifs >= P2_LIFETIME_SPEND:
                res['kills'].append((cid, layer, 'ZERO-BFC KILL', f"lifetime BOOKNOW spend Rs{lifs:,.0f}, 0 BFC ever", a)); continue
            if med_book and inst >= MIN_INSTALLS_JUNK and (bf / inst) <= (1 / JUNK_MULT) * med_book:
                ir = inst / impr if impr else 0
                kind = 'JUNK -> ITERATE' if (med_ir and ir >= med_ir) else 'JUNK -> KILL'
                tail = 'hook works, audience does not' if kind.endswith('ITERATE') else 'low intent'
                res['kills'].append((cid, layer, kind, f"{inst} inst, book {100*bf/inst:.2f}% (< half median {100*med_book:.2f}%); {tail}", a)); continue
            if med and bf >= CREATIVE_BFC_GATE:
                cpbfc = sp / bf; mult = KILL_MULT.get(layer, 1.0)
                if cpbfc >= mult * med:
                    res['kills'].append((cid, layer, 'EFFICIENCY KILL', f"CPBFC Rs{cpbfc:,.0f} >= {mult}x median (Rs{med:,.0f}); {bf} BFC, 7d spend Rs{sp:,.0f}", a)); continue
                if cpbfc <= ISOLATE_MULT * med and bf >= ISOLATE_BFC_GATE:
                    res['isolates'].append((cid, layer, f"CPBFC Rs{cpbfc:,.0f} <= 0.7x median; {bf} BFC - break into own ad set", a))
    # geo budget (Tier 2): MATURE geo only -> SCALE if <= C*, else HOLD. The mature geo is never
    # auto-CUT; above-ceiling is driven down by pruning, not by cutting the converting workhorse.
    if cstar:
        for g in MATURE_GEOS:
            wc = win.get(g, {})
            gsp = sum(x['spend'] for x in wc.values()); gbfc = sum(x['bfc'] for x in wc.values())
            if gbfc >= GEO_BUDGET_BFC_GATE:
                gcpbfc = gsp / gbfc
                res['geo_budget'].append((g, 'SCALE' if gcpbfc <= cstar else 'HOLD', gcpbfc, gbfc))
    # geo conversion problems (cold-start geos failing the conversion gate -> CAP/CUT, geo-level)
    dW = win.get('Delhi', {})
    del_inst = sum(x['inst'] for x in dW.values()); del_bfc = sum(x['bfc'] for x in dW.values())
    del_book = (del_bfc / del_inst) if del_inst else None
    for g, wc in win.items():
        if g in MATURE_GEOS or g == 'Other': continue
        ginst = sum(x['inst'] for x in wc.values()); gbfc = sum(x['bfc'] for x in wc.values()); gsp = sum(x['spend'] for x in wc.values())
        if del_book and ginst >= GEO_CONV_INSTALLS and (gbfc / ginst if ginst else 0) <= (1 / GEO_CONV_MULT) * del_book:
            res['geo_conv'].append((g, f"{ginst} installs, book {100*gbfc/ginst:.2f}% vs Delhi {100*del_book:.2f}%, 7d spend Rs{gsp:,.0f} -> serviceability, geo-level call (CAP/CUT)", gsp))
    res['kills'].sort(key=lambda x: {'ZERO-BFC KILL': 0, 'JUNK -> KILL': 1, 'JUNK -> ITERATE': 2, 'EFFICIENCY KILL': 3}[x[2]])
    return res

def msg_daily(res, cstar, start, end):
    kills = res['kills']; isos = res['isolates']
    runaway_geos = [gc for gc in res['geo_conv'] if gc[2] >= GEO_RUNAWAY_SPEND]
    if not kills and not isos and not runaway_geos:
        return f":white_check_mark: BFC-VOLUME daily check ({end}, BOOKNOW-only 7d, v3): *no creative actions today.*"
    lines = [f":mag: *BFC-VOLUME daily check - creative decisions* ({end}, BOOKNOW-only 7d, v3)",
             "_In-pool creative verdicts, run off the framework - one-click approve in Ads Manager. "
             "(Structural/geo calls are the weekly review.) Pausing is manual._", ""]
    if kills:
        lines.append(f"*Creative verdicts* ({len(kills)})" + (f"  (Delhi median CPBFC Rs{res['median']:,.0f})" if res['median'] else ""))
        for (cid, layer, rule, detail, a) in kills:
            lines.append(f"   - `{cid}` [{layer}, {a}d] *{rule}* - {detail}")
        lines.append("")
    if isos:
        lines.append("*ISOLATE candidates* (scale-out into own ad set)")
        for (cid, layer, detail, a) in isos:
            lines.append(f"   - `{cid}` [{layer}, {a}d] - {detail}")
        lines.append("")
    if runaway_geos:
        lines.append("*Circuit-breaker - geo runaway*")
        for (g, detail, _s) in runaway_geos:
            lines.append(f"   - *{g}*: {detail}")
        lines.append("")
    lines.append(f"<{ADS_MANAGER}?act=2007675312900454|Open BFC-VOLUME in Ads Manager>")
    return "\n".join(lines)

def msg_weekly(res, cstar, start, end):
    if not res['geo_budget'] and not res['geo_conv']:
        return f":memo: BFC-VOLUME weekly structural review ({start} to {end}, v3): *no geo actions.*"
    lines = [f":memo: *BFC-VOLUME weekly structural review* ({start} to {end}, BOOKNOW-only 7d, v3)",
             "_The structural / geo layer for the review with SD. In-pool creative verdicts are handled daily._", ""]
    if res['geo_budget']:
        lines.append(f"*Geo budget* (vs C* ~Rs{cstar:,.0f})" if cstar else "*Geo budget*")
        for (g, act, cp, bfc) in sorted(res['geo_budget']):
            lines.append(f"   - *{g}*: {act} - CPBFC Rs{cp:,.0f}, {bfc} BFC")
        lines.append("")
    if res['geo_conv']:
        lines.append("*Geo conversion problems* (serviceability - CAP / CUT)")
        for (g, detail, _s) in res['geo_conv']:
            lines.append(f"   - *{g}*: {detail}")
        lines.append("")
    lines.append("_Also for the weekly review (not auto-computed): L2 pool variety, L4 pipeline, fatigue->REPLACE, recharge qualifier._")
    lines.append(f"<{ADS_MANAGER}?act=2007675312900454|Open BFC-VOLUME in Ads Manager>")
    return "\n".join(lines)

def slack_api(method, token, payload):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(f'https://slack.com/api/{method}', data=data,
        headers={'Authorization': f'Bearer {token}', 'Content-Type': 'application/json; charset=utf-8'})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode())

def slack_post(text):
    token = os.environ.get('SLACK_BOT_TOKEN'); target = os.environ.get('SLACK_DM_USER_ID', 'U05A9037VFG')
    if not token: raise SystemExit("SLACK_BOT_TOKEN not set (use --dry-run to print instead).")
    channel = target
    if target.startswith('U'):                       # open the DM with the user, then post to it
        op = slack_api('conversations.open', token, {'users': target})
        if not op.get('ok'): raise SystemExit(f"Slack conversations.open failed: {op.get('error')}")
        channel = op['channel']['id']
    resp = slack_api('chat.postMessage', token, {'channel': channel, 'text': text, 'unfurl_links': False, 'mrkdwn': True})
    if not resp.get('ok'): raise SystemExit(f"Slack post failed: {resp.get('error')}")
    print("Posted to Slack DM:", channel)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['daily', 'weekly'], default='daily')
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--date', help='override D-1 anchor date YYYY-MM-DD (default = yesterday IST)')
    args = ap.parse_args()
    load_env()
    if args.date:
        d1 = datetime.date.fromisoformat(args.date)
    else:
        now_ist = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(hours=5, minutes=30)
        d1 = (now_ist - datetime.timedelta(days=1)).date()
    start = (d1 - datetime.timedelta(days=WINDOW_DAYS - 1)).isoformat(); end = d1.isoformat()
    life, win, age, cstar = compute(d1)
    res = decide(life, win, age, cstar)
    msg = (msg_daily if args.mode == 'daily' else msg_weekly)(res, cstar, start, end)
    if args.dry_run: print(msg)
    else: slack_post(msg)

if __name__ == '__main__':
    main()
