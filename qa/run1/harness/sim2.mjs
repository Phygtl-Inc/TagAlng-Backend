// Rerun failed scenarios from sim.mjs with signup throttling + JWT reuse fallback.
import { readFileSync, writeFileSync, readdirSync } from "node:fs";
import { execSync } from "node:child_process";

// Re-declare scenarios by importing sim.mjs would re-run it; instead rebuild the list
// by loading sim.mjs source and evaluating just the scenario construction.
const src = readFileSync("./sim.mjs", "utf8");
const body = src
  .slice(src.indexOf("const FL_ZIPS"), src.indexOf("// ── engine"))
  .replace("const scenarios = [];", "")
  .replaceAll("scenarios.push", "SCN.push");
const SCN = [];
eval(body);

const SUPABASE = "https://rjlcyvwogmfmngemhbmn.supabase.co";
const ANON_KEY = "sb_publishable_KLsy-8OuVq92NKWtg8bt7A_bxGUFwf2";
const WORKER = "https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app";
const OUT = "./runs";

// which scenarios still need a run?
const done = new Set(
  readdirSync(OUT)
    .filter((f) => f.endsWith(".json") && !f.startsWith("_"))
    .filter((f) => {
      try {
        const r = JSON.parse(readFileSync(`${OUT}/${f}`));
        return r.turns.length > 0 && r.errors.length === 0;
      } catch { return false; }
    })
    .map((f) => f.replace(".json", "")),
);
const todo = SCN.filter((s) => !done.has(s.id));
console.log(`todo: ${todo.length} scenarios`);

const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// JWT pool: edge scenarios get a fresh user when possible (memory isolation);
// find/host scenarios round-robin over a shared pool of anonymous users.
const pool = [];
let rr = 0;
let signupLock = Promise.resolve();
async function freshSignup() {
  let release;
  const prev = signupLock;
  signupLock = new Promise((r) => (release = r));
  await prev;
  try {
    for (let attempt = 0; attempt < 2; attempt++) {
      const r = await fetch(`${SUPABASE}/auth/v1/signup`, {
        method: "POST",
        headers: { "Content-Type": "application/json", apikey: ANON_KEY, Authorization: `Bearer ${ANON_KEY}` },
        body: "{}",
      });
      if (r.status === 429) { await sleep(12000); continue; }
      const j = await r.json();
      if (j.access_token) { await sleep(1500); pool.push(j.access_token); return j.access_token; }
    }
    return null;
  } finally { release(); }
}
async function anonJwt(isolate) {
  if (isolate || pool.length === 0) {
    const t = await freshSignup();
    if (t) return t;
  }
  if (pool.length === 0) throw new Error("no jwt available");
  return pool[rr++ % pool.length];
}

function parseSSE(text) {
  const events = [];
  for (const chunk of text.split("\n\n")) {
    const line = chunk.split("\n").filter((l) => l.startsWith("data: ")).map((l) => l.slice(6)).join("");
    if (!line) continue;
    try { events.push(JSON.parse(line)); } catch { events.push({ type: "unparseable", raw: line.slice(0, 200) }); }
  }
  return events;
}

const LEAK_PATTERNS = [
  [/[a-z]+_[a-z_]+/g, "raw_slug"],
  [/placeholder/i, "placeholder"],
  [/\bundefined\b|\bnull\b|\bNaN\b/, "null_leak"],
  [/\{\{|\}\}|\$\{/, "template_leak"],
];

async function runScenario(sc) {
  const record = { id: sc.id, tags: sc.tags, zip: sc.zip ?? null, turns: [], errors: [], startedAt: new Date().toISOString() };
  try {
    const jwt = await anonJwt(sc.tags.includes("edge"));
    const H = { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` };
    const sess = await fetch(`${WORKER}/lana/sessions`, { method: "POST", headers: H, body: "{}" }).then((r) => r.json());
    const sid = sess.session_id;
    record.session = sid;
    for (const turn of sc.turns) {
      const body = { message: turn.say };
      if (turn.hint) body.intent_hint = turn.hint;
      const t0 = Date.now();
      let res, text;
      try {
        res = await fetch(`${WORKER}/lana/sessions/${sid}/messages/stream`, {
          method: "POST", headers: { ...H, accept: "text/event-stream" }, body: JSON.stringify(body),
        });
        text = await res.text();
      } catch (e) { record.errors.push({ turn: turn.say, error: String(e) }); break; }
      const ms = Date.now() - t0;
      const events = parseSSE(text);
      const result = events.filter((e) => e.type === "result").pop();
      const t = result?.turn ?? null;
      const am = t?.assistant_message ?? "";
      const leaks = [];
      for (const [re, name] of LEAK_PATTERNS) if (re.test(am)) leaks.push(name);
      record.turns.push({
        sent: turn.say, hint: turn.hint ?? null, status: res.status, ms,
        assistant_message: am,
        routing: t?.routing ?? null,
        ui_actions: (t?.ui_actions ?? []).map((a) => a.label),
        activity_previews: (t?.activity_previews ?? []).map((p) => ({ title: p.title ?? p.name, when: p.starts_at ?? p.when, where: p.location_name ?? p.where })),
        event_draft: t?.event_draft ? { title: t.event_draft.title, starts_at: t.event_draft.starts_at, location: t.event_draft.location_name ?? t.event_draft.location } : null,
        peer_matches: (t?.peer_matches ?? []).length,
        joint_moment: !!t?.joint_moment,
        signal_saved: t?.signal_saved ?? null,
        identity_profile: t?.identity_profile ? JSON.stringify(t.identity_profile).slice(0, 400) : null,
        requires_phone_verification: t?.requires_phone_verification ?? false,
        leaks,
        statusEvents: events.filter((e) => e.type === "status").map((e) => e.label),
      });
      if (!result) { record.errors.push({ turn: turn.say, error: "no result event", raw: (text ?? "").slice(0, 300) }); break; }
    }
  } catch (e) { record.errors.push({ fatal: String(e) }); }
  record.finishedAt = new Date().toISOString();
  writeFileSync(`${OUT}/${sc.id}.json`, JSON.stringify(record, null, 2));
  return record;
}

const CONC = 2;
const queue = [...todo];
let n = 0;
async function workerLoop() {
  while (queue.length) {
    const sc = queue.shift();
    const r = await runScenario(sc);
    n++;
    console.log(`[${n}/${todo.length}] ${sc.id} · ${r.turns.length}t · err ${r.errors.length} · ${r.turns.map((t) => t.routing?.outcome ?? "?").join(">")}`);
  }
}
await Promise.all(Array.from({ length: CONC }, workerLoop));
console.log("rerun done");
