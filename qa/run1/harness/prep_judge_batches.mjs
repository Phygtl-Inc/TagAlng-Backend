// Compact all transcripts into judge-ready batches (plain text, grouped).
import { readFileSync, readdirSync, writeFileSync, mkdirSync } from "node:fs";
mkdirSync("./judge", { recursive: true });

const runs = readdirSync("./runs")
  .filter((f) => f.endsWith(".json") && !f.startsWith("_"))
  .map((f) => JSON.parse(readFileSync("./runs/" + f)))
  .filter((r) => r.turns.length > 0)
  .sort((a, b) => a.id.localeCompare(b.id));

const fmt = (r) =>
  `### ${r.id} (tags: ${r.tags.join(",")}${r.zip ? " · zip " + r.zip : ""})\n` +
  r.turns
    .map((t) => {
      let s = `MOM: ${t.sent}\nLANA: ${t.assistant_message || "(EMPTY)"}`;
      if (t.activity_previews?.length) s += `\n  [showed events: ${t.activity_previews.map((p) => `${p.title} @ ${p.where ?? "?"} ${p.when ?? ""}`).join(" | ")}]`;
      if (t.event_draft) s += `\n  [drafted event: ${JSON.stringify(t.event_draft)}]`;
      if (t.ui_actions?.length) s += `\n  [buttons: ${t.ui_actions.join(" · ")}]`;
      if (t.peer_matches) s += `\n  [peer matches: ${t.peer_matches}]`;
      return s;
    })
    .join("\n");

const groups = {};
for (const r of runs) {
  const g = r.id.startsWith("find_fl") ? "find_fl" : r.id.startsWith("find_metro") ? "find_metro" : r.id.startsWith("host") ? "host" : "edge";
  (groups[g] ??= []).push(r);
}
for (const [g, list] of Object.entries(groups)) {
  // split big groups into halves for parallel judging
  const chunks = list.length > 12 ? [list.slice(0, Math.ceil(list.length / 2)), list.slice(Math.ceil(list.length / 2))] : [list];
  chunks.forEach((chunk, i) => {
    writeFileSync(`./judge/${g}${chunks.length > 1 ? "_" + (i + 1) : ""}.md`, chunk.map(fmt).join("\n\n"));
  });
}
console.log("wrote:", readdirSync("./judge").join(", "), "· convs:", runs.length);
