/* 香港快線 — service worker.
   =========================================================================
   This is the file that makes the app installable and lets it open without a
   connection. It sits between the app and the network and decides, for every
   single request, whether a stored copy may be used.

   THE ONE RULE THAT MATTERS: a stored answer must never be able to lie to
   someone standing at a bus stop.

   So requests fall into three groups, and the group decides everything:

     NEVER STORED   arrival times, weather warnings, place search, holidays.
                    A four-minute countdown from yesterday is worse than no
                    countdown at all — the whole point of this app is that an
                    older rider can trust what it says. These go straight to
                    the network; if there is no network the app says so.

     NEVER STORED   map tiles and the Lands Department logo.
                    Not for safety — for licensing. The tiles are free to use
                    live from the Lands Department's own service, and this
                    project's rule has always been to drop anything whose
                    terms are not plainly clear. Keeping a private copy of a
                    map is a different thing from displaying it, so we don't.
                    The visible result: OFFLINE THE MAP IS BLANK. That is a
                    deliberate trade, and the app says so in plain words.

     STORED         the page, the icons, Leaflet, and the route/fare/station
                    files sitting beside this one. None of it changes during
                    a journey, all of it is needed before anything works.

   WHAT THIS MEANS OFFLINE
   The app opens, remembers your favourites, plans a journey from stored route
   data, and shows fares and station names. It cannot show live arrivals, the
   weather, or the map. That is a real, useful subset — planning "which bus do
   I take" works on the MTR platform with no signal.

   HOW TO PUSH AN UPDATE
   Change VERSION below. Browsers re-check this file on every page load and
   install the new one the moment its bytes differ, so a new VERSION both
   changes the bytes and gives the new cache a new name. The app then shows
   the rider a "new version — tap to update" button rather than reloading
   underneath them, which on a phone could throw away a journey they are
   halfway through reading.
   ========================================================================= */

const VERSION = 'hkfast-2026-09-06y';
const CACHE   = VERSION;

// Fetched at install, before the app is ever called ready. Kept small on
// purpose: this download happens on someone's mobile data. The big route and
// fare files are NOT here — the app asks for those itself on first load and
// they are stored as they arrive (see cacheFirst below).
const SHELL = [
  './hkfast.html',
  './manifest.json',
  './icon-192.png',
  './icon-512.png',
  './icon-maskable-512.png',
  './apple-touch-icon.png',
];

// Leaflet draws the map. Without it the app does not start, so it is fetched
// at install time too — but separately, because it comes from another server
// and one unreachable CDN must not be able to stop the install.
const LEAFLET = [
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.css',
  'https://unpkg.com/leaflet@1.9.4/dist/leaflet.js',
];

// Anything live, or anything whose licence we are not going to stretch.
// Matched on the exact hostname: geodata.gov.hk is the place search and
// mapapi.geodata.gov.hk is the map, and they are treated differently, so a
// loose "ends with" test would quietly do the wrong thing to one of them.
const NEVER_STORE = new Set([
  'data.etabus.gov.hk',        // KMB routes and arrival times
  'rt.data.gov.hk',            // Citybus and MTR arrival times
  'data.weather.gov.hk',       // Observatory readings and warnings
  'www.1823.gov.hk',           // public holidays
  'geodata.gov.hk',            // place search
  'mapapi.geodata.gov.hk',     // map tiles          — licensing
  'api.hkmapservice.gov.hk',   // Lands Dept logo    — licensing
  'api.tomtom.com',            // traffic tiles      — someone else's key
]);

// Third-party files that are static, versioned and safe to keep.
const STORABLE_HOSTS = new Set([
  'unpkg.com',                 // Leaflet, pinned to 1.9.4
  'static.data.gov.hk',        // Transport Department road geometry
  'raw.githubusercontent.com', // HK Bus Crawling route + fare file
]);


// ------------------------------------------------------------------ install
self.addEventListener('install', event => {
  event.waitUntil((async () => {
    const cache = await caches.open(CACHE);
    // The app's own files must all arrive or the install is worthless.
    await cache.addAll(SHELL);
    // Leaflet is added one at a time and forgiven: on a bad connection it is
    // better to have an installed app missing its map library (which the next
    // online load will fill in) than no installed app at all.
    for (const url of LEAFLET) {
      try { await cache.add(new Request(url, { mode: 'cors' })); }
      catch (e) { /* filled in later by the first successful load */ }
    }
  })());
  // Not skipWaiting(): a new worker waits until the rider chooses to update.
});


// ----------------------------------------------------------------- activate
self.addEventListener('activate', event => {
  event.waitUntil((async () => {
    for (const name of await caches.keys()) {
      if (name !== CACHE) await caches.delete(name);
    }
    await self.clients.claim();
  })());
});


// The page asks for this when the rider taps "update". Only then.
self.addEventListener('message', event => {
  if (event.data === 'SKIP_WAITING') self.skipWaiting();
});


// -------------------------------------------------------------------- fetch
self.addEventListener('fetch', event => {
  const request = event.request;

  // POST and friends are none of our business, and neither is anything the
  // page marked as no-store.
  if (request.method !== 'GET') return;

  let url;
  try { url = new URL(request.url); } catch (e) { return; }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return;

  // Live data and licensed tiles: not answered from here at all. Returning
  // without calling respondWith hands the request back to the browser
  // untouched, which is exactly what we want — no copy, no delay.
  if (NEVER_STORE.has(url.hostname)) return;

  const sameOrigin = url.origin === self.location.origin;

  // THE PAGE ITSELF: network first. Someone who is online should always get
  // the newest app, and the stored copy is the safety net, not the default.
  if (request.mode === 'navigate' || (sameOrigin && url.pathname.endsWith('.html'))) {
    event.respondWith(networkFirst(request));
    return;
  }

  if (sameOrigin || STORABLE_HOSTS.has(url.hostname)) {
    event.respondWith(cacheFirst(request));
  }
});


async function networkFirst(request) {
  const cache = await caches.open(CACHE);
  try {
    const response = await fetch(request);
    if (keepable(response)) cache.put(request, response.clone());
    return response;
  } catch (e) {
    // Offline. Any stored copy of this exact page, then the page we know we
    // stored at install — a rider opening the app from their home screen with
    // no signal must land on the app, not on the browser's dinosaur.
    return (await cache.match(request))
        || (await cache.match('./hkfast.html'))
        || new Response('Offline', { status: 503, statusText: 'Offline' });
  }
}

// Stored copy first, then quietly refresh it for next time. The route, fare
// and station files are day-stamped by the app itself in IndexedDB, so being
// one load behind costs nothing, and answering from storage is what makes the
// app open instantly instead of waiting on 2 MB of route data.
async function cacheFirst(request) {
  const cache = await caches.open(CACHE);
  const stored = await cache.match(request);

  const fromNetwork = fetch(request).then(response => {
    if (keepable(response)) cache.put(request, response.clone());
    return response;
  }).catch(() => null);

  if (stored) return stored;

  const fresh = await fromNetwork;
  return fresh || new Response('Offline', { status: 503, statusText: 'Offline' });
}

// An opaque response (a cross-origin request made without CORS) has a status
// of 0 and a body we cannot read. Storing one means storing something we
// cannot check, and it might be an error page — so only real, readable,
// successful answers are kept.
function keepable(response) {
  return response
      && response.ok
      && (response.type === 'basic' || response.type === 'cors');
}
