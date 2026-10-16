// Aggregate scoring over all run files → stats + flagged findings.
import { readFileSync, readdirSync, writeFileSync } from "node:fs";

const runs = readdirSync("./runs")
  .filter((f) => f.endsWith(".json") && !f.startsWith("_"))
  .map((f) => JSON.parse(readFileSync("./runs/" + f)));

const stats = {
  conversations: runs.length,
  completed: 0,
  failed_conversations: 0,
  total_turns: 0,
  http_errors: 0,
  empty_replies: 0,
  latency: [],
  outcomes: {},
  tools: {},
  leaks: {},
  verify_walls: 0,
  clarify_loops: 0,
  kid_name_echo: [],
  lang_mismatch: [],
  ny_bleed: 0,
  zip_dead_ends: 0,
  phone_verification_asks: 0,
  drafted_hosts: 0,
  host_draft_details: [],
  flags: [],
};

const flag = (id, kind, detail) => stats.flags.push({ id, kind, detail });

for (const r of runs) {
  if (r.errors.length && r.turns.length === 0) { stats.failed_conversations++; continue; }
  stats.completed++;
  stats.total_turns += r.turns.length;
  let zipAsks = 0;
  for (const [i, t] of r.turns.entries()) {
    stats.latency.push(t.ms);
    if (t.status !== 200) stats.http_errors++;
    if (!t.assistant_message) { stats.empty_replies++; flag(r.id, "empty_reply", `turn ${i}: "${t.sent.slice(0, 60)}"`); }
    const o = t.routing?.outcome ?? "none";
    stats.outcomes[o] = (stats.outcomes[o] || 0) + 1;
    const tool = t.routing?.tool_called;
    if (tool) stats.tools[tool] = (stats.tools[tool] || 0) + 1;
    for (const l of t.leaks) stats.leaks[l] = (stats.leaks[l] || 0) + 1;
    const am = (t.assistant_message || "").toLowerCase();
    if (am.includes("verify your email")) stats.verify_walls++;
    if (am.includes("zip")) zipAsks++;
    if (t.requires_phone_verification) stats.phone_verification_asks++;
    if (t.event_draft) {
      stats.drafted_hosts++;
      stats.host_draft_details.push({ id: r.id, sent: r.turns[0].sent.slice(0, 80), draft: t.event_draft });
    }
    // NY bleed: FL zip context but New York in previews/reply
    if (r.zip && r.zip.startsWith("3") && /new york/i.test(t.assistant_message || "")) stats.ny_bleed++;
    // kid PII echo
    if (/\bemma\b|\bjake\b|sunshine preschool/i.test(t.assistant_message || "") && r.id.startsWith("edge")) {
      stats.kid_name_echo.push({ id: r.id, msg: t.assistant_message.slice(0, 200) });
    }
  }
  if (zipAsks >= 2) { stats.zip_dead_ends++; flag(r.id, "zip_loop", `asked for ZIP ${zipAsks}x`); }
  // language check
  if (r.id === "edge_spanish" || r.id === "edge_portuguese") {
    const last = r.turns[r.turns.length - 1];
    const responded = r.turns.map((t) => t.assistant_message).join(" ");
    const looksEnglishOnly = !/[¿¡]|niñ|mamás|mães|você|cerca de ti|aquí/i.test(responded);
    if (looksEnglishOnly) stats.lang_mismatch.push({ id: r.id, sample: (last?.assistant_message || "").slice(0, 160) });
  }
}

stats.latency.sort((a, b) => a - b);
const pct = (p) => stats.latency[Math.floor((stats.latency.length - 1) * p)];
stats.latency_summary = { n: stats.latency.length, p50: pct(0.5), p90: pct(0.9), p99: pct(0.99), max: stats.latency.at(-1) };
delete stats.latency;

writeFileSync("./runs/_analysis.json", JSON.stringify(stats, null, 2));
console.log(JSON.stringify(stats, null, 2));
