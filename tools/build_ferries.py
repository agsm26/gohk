#!/usr/bin/env python3
"""Build GO HK's ferries.json from the Transport Department's GTFS feed.

    python3 tools/build_ferries.py               # download from DATA.GOV.HK
    python3 tools/build_ferries.py --dir FOLDER  # or read the .txt files from a folder
    python3 tools/build_ferries.py --check-only

SOURCE AND LICENCE. "Public transport routes, fares and timetables (GTFS)"
by the Transport Department, DATA.GOV.HK — https://static.data.gov.hk/td/
pt-headway-en/ and pt-headway-tc/. Free for any use; the source must be
acknowledged.

WHY THE GOVERNMENT FEED AND NOT HK BUS CRAWLING. The crawler's file carries
29 ferry routes and no sailing times. The TD feed has every licensed ferry
(55 routes: Star Ferry, the outlying islands, Discovery Bay, Ma Wan, Tap Mun,
Po Toi…) and, for every sailing, when it leaves and when it arrives — which
is what lets a trip plan say how long the crossing takes instead of guessing
from a bus speed.

WHAT THE FEED SAYS AND DOES NOT SAY. Times are given at the first and last
pier of each sailing; piers in between have none. Their minutes are spread
by distance along the route and are therefore approximate, and the app says
so. One fare per pier pair is published; where an operator also runs a
dearer fast ferry on the same route, the feed carries the ordinary sailing.

OUTPUT (what gohk.html reads — change both together or neither)
  stops:  { id: [name EN, name ZH, lat, lng] }
  routes: [{ r: route id, b: "O" (direction 1) | "I" (direction 2),
             n: [name EN, name ZH], s: [pier ids in order],
             m: [minutes after leaving the first pier, per pier],
             f: [fare boarding at each pier, to the last one] (null = none),
             q: { service id: { "HHMM": null }                 one sailing
                              | { "HHMM": ["HHMM", "secs"] } } every N secs }]

SAFETY. Nothing is written unless the checks pass (at least 25 routes, every
pier inside Hong Kong, every crossing between 2 and 180 minutes, no fewer
routes than 90% of the file already here).
"""
import argparse
import collections
import csv
import datetime
import io
import json
import math
import os
import re
import sys
import urllib.request

BASE = 'https://static.data.gov.hk/td/pt-headway-%s/'
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, 'ferries.json')
SOURCE = ('Transport Department, Public transport routes, fares and timetables (GTFS), '
          'DATA.GOV.HK — https://data.gov.hk/en-data/dataset/hk-td-tis_11-pt-headway-en')
LAT_RANGE = (22.13, 22.58)
LNG_RANGE = (113.80, 114.45)


def open_text(args, lang, name):
    """The file as text lines, from a folder or from DATA.GOV.HK."""
    if args.dir:
        path = os.path.join(args.dir, lang, name)
        with open(path, encoding='utf-8-sig') as fh:
            return fh.read()
    with urllib.request.urlopen(BASE % lang + name, timeout=300) as response:
        return response.read().decode('utf-8-sig')


def rows(text, keep=None):
    reader = csv.reader(io.StringIO(text))
    header = next(reader)
    for row in reader:
        if not row:
            continue
        record = dict(zip(header, row))
        if keep is None or keep(record):
            yield record


def pier_name(name):
    """The feed joins a pier's names with "|" — two berths ("…碼頭 (西面泊位）|
    …碼頭 (東面泊位）") or a long and a short name ("塔門碼頭|塔門"). One name
    is shown: the berth brackets dropped, then the most specific (longest)."""
    if '|' not in name:
        return name.strip()
    parts = [re.sub(r'\s*[(（][^()（）]*[)）]\s*$', '', p).strip() for p in name.split('|')]
    parts = [p for p in parts if p]
    return max(parts, key=len) if parts else name.strip()


def service_mask(service_id):
    """The TD's service ids are day masks (bit0 Monday … bit6 Sunday, bit7
    public holidays). Bit 8 changes nothing in the calendar; the app reads it
    as "from the TD feed, so a missing holiday bit means no service on public
    holidays". Every ferry row IS from the TD feed, so every one gets it."""
    try:
        return str(int(service_id) | 256)
    except ValueError:
        return service_id


def hhmm(value):
    """'14:05:00' -> '1405'; past midnight stays past 24, as the feed writes it."""
    h, m = value.strip().split(':')[:2]
    return '%02d%s' % (int(h), m)


def minutes(value):
    h, m = value.strip().split(':')[:2]
    return int(h) * 60 + int(m)


def metres(a, b):
    lat = math.radians((a[0] + b[0]) / 2)
    dx = (b[1] - a[1]) * 111320 * math.cos(lat)
    dy = (b[0] - a[0]) * 110540
    return math.hypot(dx, dy)


def build(args):
    routes_en = {r['route_id']: r for r in rows(open_text(args, 'en', 'routes.txt'),
                                                 lambda r: r.get('route_type') == '4')}
    routes_tc = {r['route_id']: r for r in rows(open_text(args, 'tc', 'routes.txt'),
                                                 lambda r: r['route_id'] in routes_en)}
    trips = {r['trip_id']: r for r in rows(open_text(args, 'en', 'trips.txt'),
                                           lambda r: r['route_id'] in routes_en)}
    times = collections.defaultdict(list)
    for r in rows(open_text(args, 'en', 'stop_times.txt'), lambda r: r['trip_id'] in trips):
        times[r['trip_id']].append(r)
    freqs = collections.defaultdict(list)
    for r in rows(open_text(args, 'en', 'frequencies.txt'), lambda r: r['trip_id'] in trips):
        freqs[r['trip_id']].append(r)
    stop_ids = {r['stop_id'] for rs in times.values() for r in rs}
    stops_en = {r['stop_id']: r for r in rows(open_text(args, 'en', 'stops.txt'),
                                              lambda r: r['stop_id'] in stop_ids)}
    stops_tc = {r['stop_id']: r for r in rows(open_text(args, 'tc', 'stops.txt'),
                                              lambda r: r['stop_id'] in stop_ids)}
    prefixes = tuple(rid + '-' for rid in routes_en)
    fares = {r['fare_id']: r['price'] for r in rows(open_text(args, 'en', 'fare_attributes.txt'),
                                                    lambda r: r['fare_id'].startswith(prefixes))}

    # Group sailings by route and direction (trip ids read ROUTE-DIR-SERVICE-HHMM).
    groups = collections.defaultdict(list)
    for trip_id, trip in trips.items():
        rs = sorted(times.get(trip_id, []), key=lambda r: int(r['stop_sequence']))
        if len(rs) < 2:
            continue
        direction = trip_id[len(trip['route_id']) + 1:].split('-')[0]
        groups[(trip['route_id'], direction)].append((trip, rs))

    out_routes, used_stops = [], set()
    for (route_id, direction), sailings in sorted(groups.items()):
        # The pier sequence most sailings follow. A sailing that skips or adds
        # a pier is a different service, and mixing them would put piers in the
        # wrong order; it is left out rather than guessed at.
        patterns = collections.Counter(tuple(r['stop_id'] for r in rs) for _, rs in sailings)
        pattern = patterns.most_common(1)[0][0]
        keep = [(t, rs) for t, rs in sailings if tuple(r['stop_id'] for r in rs) == pattern]
        if any(p not in stops_en for p in pattern):
            continue
        coords = [(float(stops_en[p]['stop_lat']), float(stops_en[p]['stop_lon'])) for p in pattern]

        # Crossing time: first pier's departure to last pier's arrival, the
        # most common value over all its sailings.
        crossing = collections.Counter()
        for _, rs in keep:
            first = next((r for r in rs if r['departure_time']), None)
            last = next((r for r in reversed(rs) if r['arrival_time']), None)
            if first and last:
                crossing[minutes(last['arrival_time']) - minutes(first['departure_time'])] += 1
        if not crossing:
            continue
        total = crossing.most_common(1)[0][0]
        # Piers in between: spread by distance along the route.
        dist = [0.0]
        for a, b in zip(coords, coords[1:]):
            dist.append(dist[-1] + metres(a, b))
        offsets = [round(total * d / dist[-1]) if dist[-1] else 0 for d in dist]
        offsets[-1] = total

        timetable = collections.defaultdict(dict)
        for trip, rs in keep:
            service = service_mask(trip['service_id'])
            if trip['trip_id'] in freqs:
                for f in freqs[trip['trip_id']]:
                    timetable[service][hhmm(f['start_time'])] = [hhmm(f['end_time']), str(int(f['headway_secs']))]
            else:
                first = next((r for r in rs if r['departure_time']), None)
                if first:
                    timetable[service][hhmm(first['departure_time'])] = None
        timetable = {svc: dict(sorted(t.items())) for svc, t in sorted(timetable.items(), key=lambda kv: int(kv[0]))}

        n = len(pattern)
        fare_list = []
        for i in range(1, n + 1):
            price = fares.get('%s-%s-%d-%d' % (route_id, direction, i, n))
            try:
                value = float(price) if price is not None else None
            except ValueError:
                value = None
            fare_list.append(value if value else None)

        en, tc = routes_en[route_id], routes_tc.get(route_id, {})
        out_routes.append({
            'r': route_id, 'b': 'O' if direction == '1' else 'I',
            'n': [en.get('route_long_name', ''), tc.get('route_long_name', '')],
            's': list(pattern), 'm': offsets,
            'f': fare_list[:-1] if len(fare_list) > 1 else fare_list,
            'q': timetable,
        })
        used_stops.update(pattern)

    out_stops = {}
    for sid in sorted(used_stops):
        e, t = stops_en[sid], stops_tc.get(sid, {})
        out_stops[sid] = [pier_name(e.get('stop_name', '')), pier_name(t.get('stop_name', '')),
                          round(float(e['stop_lat']), 5), round(float(e['stop_lon']), 5)]
    return {'generated': datetime.date.today().isoformat(), 'source': SOURCE,
            'stops': out_stops, 'routes': out_routes}


def check(data):
    problems = []
    try:
        with open(OUT, encoding='utf-8') as fh:
            old = json.load(fh)
        if len(data['routes']) < 0.9 * len(old['routes']):
            problems.append('routes fell from %d to %d' % (len(old['routes']), len(data['routes'])))
    except (OSError, ValueError):
        pass
    if len(data['routes']) < 25:
        problems.append('only %d ferry routes' % len(data['routes']))
    for sid, (en, zh, lat, lng) in data['stops'].items():
        if not (LAT_RANGE[0] <= lat <= LAT_RANGE[1] and LNG_RANGE[0] <= lng <= LNG_RANGE[1]):
            problems.append('pier %s (%s) is outside Hong Kong' % (sid, zh or en))
    for r in data['routes']:
        if not (2 <= r['m'][-1] <= 180):
            problems.append('route %s-%s crosses in %d minutes' % (r['r'], r['b'], r['m'][-1]))
        if len(r['s']) != len(r['m']) or len(r['s']) < 2:
            problems.append('route %s-%s has a broken pier list' % (r['r'], r['b']))
        if not r['q']:
            problems.append('route %s-%s has no sailings' % (r['r'], r['b']))
        if any(f is not None and not (0 < f < 500) for f in r['f']):
            problems.append('route %s-%s has an impossible fare' % (r['r'], r['b']))
    return problems


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dir', help='folder holding en/ and tc/ GTFS .txt files')
    parser.add_argument('--check-only', action='store_true')
    args = parser.parse_args()
    data = build(args)
    print('%d ferry routes, %d piers, %d sailings or windows'
          % (len(data['routes']), len(data['stops']),
             sum(len(t) for r in data['routes'] for t in r['q'].values())))
    problems = check(data)
    if problems:
        print('NOT WRITTEN — %d problem(s):' % len(problems))
        for p in problems[:40]:
            print('  -', p)
        sys.exit(1)
    if args.check_only:
        print('checks passed (nothing written)')
        return
    text = json.dumps(data, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
    with open(OUT + '.tmp', 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(OUT + '.tmp', OUT)
    print('written: ferries.json')


if __name__ == '__main__':
    main()
