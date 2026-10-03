// Single-conversation probe: learn the worker's SSE format before scaling up.
const SUPABASE = "https://rjlcyvwogmfmngemhbmn.supabase.co";
const ANON_KEY = "sb_publishable_KLsy-8OuVq92NKWtg8bt7A_bxGUFwf2";
const WORKER = "https://tagalng-lana-worker-s5gmxb6whq-ue.a.run.app";

async function main() {
  // 1. anonymous signup
  const auth = await fetch(`${SUPABASE}/auth/v1/signup`, {
    method: "POST",
    headers: { "Content-Type": "application/json", apikey: ANON_KEY, Authorization: `Bearer ${ANON_KEY}` },
    body: JSON.stringify({}),
  }).then((r) => r.json());
  const jwt = auth.access_token;
  if (!jwt) throw new Error("no jwt: " + JSON.stringify(auth).slice(0, 300));
  console.log("JWT ok, user", auth.user?.id);

  const H = { "Content-Type": "application/json", Authorization: `Bearer ${jwt}` };

  // 2. create session
  const sess = await fetch(`${WORKER}/lana/sessions`, { method: "POST", headers: H, body: "{}" }).then((r) => r.json());
  console.log("session:", JSON.stringify(sess).slice(0, 500));
  const sid = sess.id ?? sess.session_id ?? sess.session?.id;

  // 3. one message, dump raw SSE
  const res = await fetch(`${WORKER}/lana/sessions/${sid}/messages/stream`, {
    method: "POST",
    headers: { ...H, accept: "text/event-stream" },
    body: JSON.stringify({ message: "I'm looking for a meet or playgroup", intent_hint: "look_meet" }),
  });
  console.log("stream status:", res.status);
  const text = await res.text();
  console.log("RAW SSE >>>");
  console.log(text.slice(0, 4000));
}
main().catch((e) => { console.error(e); process.exit(1); });
