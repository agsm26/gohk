#!/usr/bin/env python3
"""Rebuild GO HK's two bus data files from HK Bus Crawling's routeFareList.

    python3 tools/build_data.py                 # download the latest source
    python3 tools/build_data.py --source FILE   # or use a file already on disk
    python3 tools/build_data.py --check-only    # build and check, write nothing

Writes, beside gohk.html:
  hk-bus-lite.json  Citybus, NLB, green minibus, MTR Bus and ferry routes,
                    their stops and fares (the app's only source for these)
  kmb-fares.json    KMB / Long Win fares and published service windows

SOURCE AND LICENCE. routeFareList.min.json is the OUTPUT of HK Bus Crawling
(github.com/hkbus/hk-bus-crawling), compiled from the Transport Department's
open data on DATA.GOV.HK — free for any use with attribution. The crawler is
GPL-2.0; its output is not, and inherits DATA.GOV.HK's terms. Credit it as
"HK Bus Crawling@2021".

SAFETY. Nothing is written unless every check in check() passes: route and
stop counts may not fall more than 10% below the files already here, every
route needs two or more stops that exist, every stop must sit inside Hong
Kong, every fare must be a plausible number. A refresh that fails a check
leaves the old files exactly as they were and exits non-zero, so the monthly
job (.github/workflows/refresh-data.yml) commits nothing.

FORMATS (what gohk.html reads — change both together or neither):
  hk-bus-lite.json
    routes: [{ r: route number, t: service type (string),
               b: {company: bound}, s: {company: [stop ids]},
               i: NLB's own routeId (NLB only) — its live times are asked by it
               f: [fare boarding at stop 1, 2, ...]   (omitted if none)
               h: [the same on Sundays and public holidays] (omitted if none)
               q: published service windows            (omitted if none) }]
    stops:  { id: [name EN, name ZH, lat, lng] }
  kmb-fares.json
    fares: { "ROUTE|BOUND|TYPE": [fare boarding at seq 1, 2, ...] }
    freq:  { "ROUTE|BOUND|TYPE": published service windows }

TWO RULES THAT ARE NOT OBVIOUS
  * A fare of 0 is "no fare published", written as null — never as free.
  * No two routes may share a ROUTE|BOUND|TYPE|COMPANY key. NLB files both
    directions of most routes under bound "O", and green minibus route "1"
    exists in three regions; with shared keys the app kept the first and lost
    the rest (198 variants, Sep 2026). A repeat gets its type suffixed:
    "1", then "1v2", "1v3"… The first keeps the plain type, so a bus a rider
    saved before this change still opens the same route.
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

SOURCE_URL = 'https://raw.githubusercontent.com/hkbus/hk-bus-crawling/gh-pages/routeFareList.min.json'
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Companies whose routes, stops and fares travel in hk-bus-lite.json.
# KMB is not here: its stops come from KMB's own API, its fares from
# kmb-fares.json. Nor are the ferries: the crawler has 29 of them and no
# sailing times, so they come from the Transport Department's own feed
# instead — see build_ferries.py.
LITE_COMPANIES = ('ctb', 'nlb', 'gmb', 'lrtfeeder')

# Hong Kong, generously: a stop outside this box is a broken coordinate.
LAT_RANGE = (22.13, 22.58)
LNG_RANGE = (113.80, 114.45)

LITE_SOURCE = ('Transport Department open data via data.gov.hk, compiled by HK Bus '
               'Crawling@2021 (https://github.com/hkbus/hk-bus-crawling). Citybus, New '
               'Lantao Bus, green minibus and MTR Bus — KMB comes from the KMB open API.')
KMB_SOURCE = ('Transport Department GTFS fare_attributes + timetables via data.gov.hk, '
              'compiled by HK Bus Crawling@2021 (https://github.com/hkbus/hk-bus-crawling)')
KMB_NOTE = ('fares["route|bound|service_type"][seq-1] = adult HKD boarding at that stop '
            'to the terminus. null = none published. freq = published service windows.')


def fare_list(raw):
    """Strings to numbers; 0 (none published) to None. None for an empty list."""
    if not raw:
        return None
    out = []
    for value in raw:
        try:
            number = float(value)
        except (TypeError, ValueError):
            number = None
        out.append(number if number else None)
    return out if any(v is not None for v in out) else None


def build_lite(source):
    routes, used_keys, stop_ids = [], set(), set()
    for entry in source['routeList'].values():
        companies = [c for c in entry.get('co', [])
                     if c in LITE_COMPANIES and c in entry.get('bound', {})
                     and len(entry.get('stops', {}).get(c, [])) >= 2]
        if not companies:
            continue
        base_type = str(entry.get('serviceType', '1'))
        route = str(entry['route'])

        def keys_for(t):
            return [route + '|' + entry['bound'][c] + '|' + t + '|' + c for c in companies]

        service_type, n = base_type, 1
        while any(k in used_keys for k in keys_for(service_type)):
            n += 1
            service_type = base_type + 'v' + str(n)
        used_keys.update(keys_for(service_type))

        record = {'r': route, 't': service_type,
                  'b': {c: entry['bound'][c] for c in companies},
                  's': {c: entry['stops'][c] for c in companies}}
        # NLB answers live times per routeId, and files both directions of a
        # route under bound "O": without the id the app has to guess which of
        # six "route 1" variants a bus belongs to.
        if 'nlb' in companies and entry.get('nlbId'):
            record['i'] = str(entry['nlbId'])
        fares = fare_list(entry.get('fares'))
        if fares:
            record['f'] = fares
        holiday = fare_list(entry.get('faresHoliday'))
        if holiday and holiday != fares:
            record['h'] = holiday
        if entry.get('freq'):
            record['q'] = entry['freq']
        routes.append(record)
        for c in companies:
            stop_ids.update(entry['stops'][c])

    stops = {}
    stop_list = source['stopList']
    for sid in sorted(stop_ids):
        s = stop_list.get(sid)
        if not s:
            continue
        stops[sid] = [s['name'].get('en', ''), s['name'].get('zh', ''),
                      s['location']['lat'], s['location']['lng']]
    return {'generated': datetime.date.today().isoformat(), 'source': LITE_SOURCE,
            'routes': routes, 'stops': stops}


def build_kmb(source):
    """One entry per KMB ROUTE|BOUND|TYPE. When several source records share a
    key, the KMB-only one wins over a jointly run one (they carry the same
    KMB stop list; checked against KMB's own API on 2 Oct 2026: 1,601 of
    1,605 keys have identical stop sequences)."""
    picked = {}
    for entry in source['routeList'].values():
        if 'kmb' not in entry.get('co', []) or 'kmb' not in entry.get('bound', {}):
            continue
        if not entry.get('stops', {}).get('kmb'):
            continue
        key = str(entry['route']) + '|' + entry['bound']['kmb'] + '|' + str(entry.get('serviceType', '1'))
        pure = entry['co'] == ['kmb']
        if key not in picked or (pure and not picked[key][0]):
            picked[key] = (pure, entry)
    fares, freq, holiday = {}, {}, {}
    for key, (_, entry) in picked.items():
        f = fare_list(entry.get('fares'))
        if f:
            fares[key] = f
        h = fare_list(entry.get('faresHoliday'))
        if h and h != f:
            holiday[key] = h
        if entry.get('freq'):
            freq[key] = entry['freq']
    out = {'generated': datetime.date.today().isoformat(), 'source': KMB_SOURCE,
           'note': KMB_NOTE, 'fares': fares, 'freq': freq}
    if holiday:
        out['faresHoliday'] = holiday
    return out


def load_existing(name):
    try:
        with open(os.path.join(ROOT, name), encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def check(lite, kmb):
    problems = []
    old_lite, old_kmb = load_existing('hk-bus-lite.json'), load_existing('kmb-fares.json')
    if old_lite:
        if len(lite['routes']) < 0.9 * len(old_lite['routes']):
            problems.append('routes fell from %d to %d' % (len(old_lite['routes']), len(lite['routes'])))
        if len(lite['stops']) < 0.9 * len(old_lite['stops']):
            problems.append('stops fell from %d to %d' % (len(old_lite['stops']), len(lite['stops'])))
    if old_kmb:
        if len(kmb['fares']) < 0.9 * len(old_kmb['fares']):
            problems.append('KMB fares fell from %d to %d' % (len(old_kmb['fares']), len(kmb['fares'])))
        if len(kmb['freq']) < 0.9 * len(old_kmb['freq']):
            problems.append('KMB timetables fell from %d to %d' % (len(old_kmb['freq']), len(kmb['freq'])))
    if len(lite['routes']) < 1500 or len(lite['stops']) < 5000 or len(kmb['fares']) < 1000:
        problems.append('far too little data: %d routes, %d stops, %d KMB fares'
                        % (len(lite['routes']), len(lite['stops']), len(kmb['fares'])))
    seen = set()
    for r in lite['routes']:
        for company, ids in r['s'].items():
            key = r['r'] + '|' + r['b'][company] + '|' + r['t'] + '|' + company
            if key in seen:
                problems.append('duplicate key ' + key)
            seen.add(key)
            if len(ids) < 2:
                problems.append('route %s has fewer than two stops' % key)
            missing = [i for i in ids if i not in lite['stops']]
            if missing:
                problems.append('route %s has %d unknown stops' % (key, len(missing)))
        for field in ('f', 'h'):
            for value in r.get(field) or []:
                if value is not None and not (0 < value < 200):
                    problems.append('route %s|%s has fare %r' % (r['r'], r['t'], value))
                    break
    for sid, (en, zh, lat, lng) in lite['stops'].items():
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
            problems.append('stop %s (%s) is outside Hong Kong: %s,%s' % (sid, zh or en, lat, lng))
        if not (en or zh):
            problems.append('stop %s has no name' % sid)
    for field in ('fares', 'faresHoliday'):
        for key, values in (kmb.get(field) or {}).items():
            if any(v is not None and not (0 < v < 200) for v in values):
                problems.append('KMB %s has an impossible %s value' % (key, field))
    nlb = [r for r in lite['routes'] if 'nlb' in r['b']]
    if nlb and sum(1 for r in nlb if r.get('i')) < 0.9 * len(nlb):
        problems.append('only %d of %d NLB routes carry their NLB routeId' % (sum(1 for r in nlb if r.get('i')), len(nlb)))
    return problems


def summary(lite, kmb):
    by_company = {}
    for r in lite['routes']:
        for c in r['b']:
            by_company[c] = by_company.get(c, 0) + 1
    holiday = sum(1 for r in lite['routes'] if 'h' in r)
    nlb_ids = sum(1 for r in lite['routes'] if 'i' in r)
    suffixed = sum(1 for r in lite['routes'] if 'v' in r['t'])
    return ('%d routes %s, %d stops, %d with holiday fares, %d variants given their own key, '
            '%d NLB routeIds; KMB: %d fares, %d timetables'
            % (len(lite['routes']), by_company, len(lite['stops']), holiday, suffixed, nlb_ids,
               len(kmb['fares']), len(kmb['freq'])))


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n')[0])
    parser.add_argument('--source', help='routeFareList.min.json on disk (default: download)')
    parser.add_argument('--check-only', action='store_true', help='build and check, write nothing')
    args = parser.parse_args()

    if args.source:
        with open(args.source, encoding='utf-8') as fh:
            source = json.load(fh)
    else:
        with urllib.request.urlopen(SOURCE_URL, timeout=120) as response:
            source = json.load(response)

    lite, kmb = build_lite(source), build_kmb(source)
    print(summary(lite, kmb))
    problems = check(lite, kmb)
    if problems:
        print('NOT WRITTEN — %d problem(s):' % len(problems))
        for p in problems[:40]:
            print('  -', p)
        sys.exit(1)
    if args.check_only:
        print('checks passed (nothing written)')
        return
    # Both files are written whole or not at all: serialised first (NaN is
    # refused, not written as a bare NaN the app cannot parse), then each is
    # swapped in from a temporary file.
    texts = {name: json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
             for name, data in (('hk-bus-lite.json', lite), ('kmb-fares.json', kmb))}
    for name, text in texts.items():
        path = os.path.join(ROOT, name)
        with open(path + '.tmp', 'w', encoding='utf-8') as fh:
            fh.write(text)
        os.replace(path + '.tmp', path)
    print('written: hk-bus-lite.json, kmb-fares.json')


if __name__ == '__main__':
    main()
