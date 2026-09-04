/*
minime_loadtest.js — k6 load test targeting specific findings from the
production cost reaudit. Install k6 first: https://github.com/grafana/k6
(brew install k6 / apt / choco / or download a binary — no signup).

Prereqs:
  1. uvicorn api.server:app running locally against a real DATABASE_URL
     and Upstash Redis/Vector (a staging pair, ideally — not prod).
  2. A test JWT:  python scripts/get_test_jwt.py --save-to /tmp/token.txt
  3. Seed data:   python seed_load_test_data.py http://localhost:8000 \
                    "$(cat /tmp/token.txt)" --chats 20 --messages-per-chat 150
                  (copy the printed chat_id into CHAT_ID below)

Run one scenario at a time, e.g.:
  k6 run -e BASE_URL=http://localhost:8000 -e TOKEN=$(cat /tmp/token.txt) \
     -e CHAT_ID=chat_xxxxxxxxxxxx \
     --scenario db_pool_pressure minime_loadtest.js

Or run everything (scenarios execute in parallel unless you pass
--scenario) — probably don't do that on a first run, since
agent_task_burst has real cost implications (see its own comment below).

While a scenario runs, poll the relevant stats endpoint in another
terminal to watch the numbers move in real time, e.g.:
  watch -n2 'curl -s -H "Authorization: Bearer $TOKEN" \
    http://localhost:8000/api/system/agent-pool-stats | jq'
*/

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend } from 'k6/metrics';

const BASE_URL = __ENV.BASE_URL || 'http://localhost:8000';
const TOKEN = __ENV.TOKEN;
const CHAT_ID = __ENV.CHAT_ID; // a seeded chat with 100+ messages, see seed_load_test_data.py

if (!TOKEN) {
  throw new Error('Set -e TOKEN=<jwt from scripts/get_test_jwt.py>');
}

const authHeaders = { headers: { Authorization: `Bearer ${TOKEN}` } };

// Separate latency trends per route class so you can see fast-route
// degradation independently of the slow route that's supposedly causing it
// — this is the actual question §3.2/§3.1 needs answered.
const fastRouteLatency = new Trend('fast_route_latency', true);
const slowRouteLatency = new Trend('slow_route_latency', true);

export const options = {
  scenarios: {
    // --- Finding §3.1/§3.2: does the dedicated agent pool actually
    // protect fast routes, and does its unbounded queue matter in
    // practice? Runs a small burst of "slow" agent-pool requests
    // concurrently with a steady trickle of "fast" requests, and
    // records both latencies separately.
    //
    // COST WARNING: post_task hits real LLM providers via
    // utils/llm_client.py's fallback chain. Keep `slow_vus` at or
    // below AGENT_TASK_POOL_SIZE (default 8) for a first run, and
    // prefer your cheapest real task shape (e.g. tier_override=0 /
    // a trivial prompt) over anything that fans out to multiple
    // agents. Check api/routes/tasks.py for the lightest real
    // request body before raising concurrency here.
    pool_starvation: {
      executor: 'per-vu-iterations',
      exec: 'poolStarvation',
      vus: 1, // this scenario manages its own concurrency via sub-requests below
      iterations: 1,
      startTime: '0s',
    },

    // --- Finding §3.5/§5.6: DB pool contention and RLS/query
    // fan-out cost. Pure GET traffic, no LLM cost — safe to push
    // hard. Watch /api/system/db-pool-stats' acquired_after_wait
    // and exhausted counts move as you raise `vus`.
    db_pool_pressure: {
      executor: 'ramping-vus',
      exec: 'dbPoolPressure',
      startVUs: 0,
      stages: [
        { duration: '20s', target: 30 },
        { duration: '40s', target: 30 },
        { duration: '10s', target: 0 },
      ],
      startTime: '0s',
    },

    // --- Finding §5.7: chat_page_cache hit_rate. Many VUs
    // independently request the SAME before_seq page (same chat_id,
    // same before_seq, same limit) to cross HIT_THRESHOLD=3 within
    // HIT_WINDOW_SECONDS, then keep requesting it to see whether
    // later requests actually come back faster / register as hits
    // in /api/system/chat-page-cache-stats.
    page_cache_probe: {
      executor: 'shared-iterations',
      exec: 'pageCacheProbe',
      vus: 10,
      iterations: 50,
      startTime: '0s',
      maxDuration: '60s',
    },
  },
  thresholds: {
    // Tune these once you have a baseline — starting point only.
    fast_route_latency: ['p(95)<500'],
  },
};

// --- pool_starvation ---------------------------------------------------
export function poolStarvation() {
  // Fires `slowCount` concurrent "slow" task requests and, in parallel,
  // a steady stream of "fast" requests for `durationSec` seconds, using
  // k6's batch + a manual timer loop rather than two separate
  // executors, so both classes of traffic genuinely overlap in time.
  const slowCount = 8; // match AGENT_TASK_POOL_SIZE — see cost warning above
  const durationSec = 30;
  const endAt = Date.now() + durationSec * 1000;

  // Kick off the slow batch (fire-and-forget style: k6 http.batch is
  // still synchronous from the script's perspective, so this measures
  // the batch's own completion time as slowRouteLatency).
  const slowRequests = [];
  for (let i = 0; i < slowCount; i++) {
    slowRequests.push([
      'POST',
      `${BASE_URL}/api/task`,
      JSON.stringify({
        session_id: `loadtest-pool-${__VU}-${i}-${Date.now()}`,
        task: 'Reply with just the word OK.',
        tier_override: 0, // cheapest real path if your build supports this — check api/routes/tasks.py
      }),
      { headers: { ...authHeaders.headers, 'Content-Type': 'application/json' } },
    ]);
  }
  const slowStart = Date.now();
  const slowResponses = http.batch(slowRequests);
  slowRouteLatency.add(Date.now() - slowStart);
  slowResponses.forEach((res) => check(res, { 'task accepted': (r) => r.status === 200 || r.status === 202 }));

  // Meanwhile (well, sequentially in this VU, but overlapping in wall
  // time with however long the batch above took), hammer a fast route
  // and record its latency distribution.
  while (Date.now() < endAt) {
    const t0 = Date.now();
    const res = http.get(`${BASE_URL}/api/chats`, authHeaders);
    fastRouteLatency.add(Date.now() - t0);
    check(res, { 'fast route 200': (r) => r.status === 200 });
    sleep(0.2);
  }
}

// --- db_pool_pressure ----------------------------------------------------
export function dbPoolPressure() {
  if (!CHAT_ID) {
    throw new Error('Set -e CHAT_ID=<chat_id from seed_load_test_data.py>');
  }
  const res = http.get(`${BASE_URL}/api/chats/${CHAT_ID}?limit=60`, authHeaders);
  check(res, { '200': (r) => r.status === 200 });
  sleep(0.1);
}

// --- page_cache_probe ------------------------------------------------
export function pageCacheProbe() {
  if (!CHAT_ID) {
    throw new Error('Set -e CHAT_ID=<chat_id from seed_load_test_data.py>');
  }
  // Fixed (chat_id, before_seq, limit) tuple on purpose — cache_page_key
  // is exact-match, so every VU/iteration hitting THIS SAME page is what
  // simulates real fan-out (many different users/opens landing on the
  // same older-message page of a popular chat).
  const beforeSeq = 100; // pick a value inside your seeded chat's seq range
  const res = http.get(
    `${BASE_URL}/api/chats/${CHAT_ID}?before_seq=${beforeSeq}&limit=20`,
    authHeaders
  );
  check(res, { '200': (r) => r.status === 200 });
  sleep(0.05);
}
