# Running the Lana sim suite against a LOCAL Supabase stack

**Evals and dev only.** Nothing here is a deployment path, a staging path, or a way to test
against production. It exists so that running the simulation suite stops being a shared,
destructive act.

---

## Setup checklist — verified state as of 2026-09-07

Two of the four prerequisites are already done on this machine. Only Docker is outstanding.

| | Prerequisite | State | If missing |
|---|---|---|---|
| 1 | Python deps | **done** — `httpx 0.28.1`, `openai 2.50.0`, `pydantic 2.13.4`, `python-dotenv 1.2.2`, `PyYAML 6.0.2`, `matplotlib 3.10.1` | `pip install -r services/lana-worker/simulations/requirements.txt` |
| 2 | Supabase CLI | **done** — `2.116.0`, and it parses this repo's `config.toml` (`project_id = "tagalng"`) | `npm install -g supabase` |
| 3 | **Docker** | **MISSING — the only blocker** | see below |
| 4 | Repo assets | **done** — 177 migrations + `supabase/seed_sim_accounts.sql` already in the tree | — |

### 3. Docker — needs a human

Not installed here, and not something to install unattended: it wants admin rights, enables WSL2,
usually asks for a reboot, and Docker Desktop carries per-organisation licence terms that are a
company decision rather than a tooling one.

```powershell
winget install Docker.DockerDesktop      # then reboot, and launch it once so the engine starts
```

Podman works too — the CLI checks for both (`docker: command not found (podman also not found)`).

Verify with `docker info`, then `supabase status` should stop complaining about the engine.

### Then, in order

```powershell
supabase start                                   # boots Postgres+GoTrue+PostgREST, applies all 177 migrations
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" -v sim_password='<choose one>' -f supabase/seed_sim_accounts.sql
```

Point BOTH env files at local — the harness reads `.env.local`, the worker reads
`deploy/lana-worker.env`, and if they disagree on `SUPABASE_URL` the worker rejects the harness's
JWT and every run is meaningless:

```
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_ANON_KEY=<from `supabase status`>
SUPABASE_SERVICE_ROLE_KEY=<from `supabase status`>
SIM_PASSWORD=<the one you chose above>
```

### Verify before trusting a run

```powershell
cd services/lana-worker/simulations
python -c "import local_guard; print(local_guard.classify(__import__('os').environ['SUPABASE_URL']))"   # must print: local
python selftest.py                                    # guard cases must still pass
python runner.py --pr --dry-run                       # 33 cases, no API calls
```

`classify` printing `local` is the one that matters — it is what flips `require_local` from
refusing to allowing, and it is why no `SIM_ALLOW_NONLOCAL_WRITES=1` should ever appear in a local
command line. If you find yourself typing that flag, you are still pointed at the shared project.

### What this unlocks, to justify the Docker install

- the 27 `policy_eval` scenarios currently `UNSCORED` — the world can be pinned for real
- `circles_zip`'s live sweep — 150 synthetic moms can be materialised, which is impossible on a
  shared project
- no sim-account contention with anyone else's run
- seeded supply (events, communities, neighbours) for Asjid's empty-state spec


---

## Why (read this once — it explains every guard below)

All six sim persona accounts (`p1-sim … p6-sim@phygtl.dev`) live in the **shared dev project**.
Two consequences:

1. **Sessions collide.** `create_session(force_new=True)` calls `abandon_other_active_sessions`
   (`app/db.py:340,363`). Two people running the suite at the same time kill each other's
   sessions — the symptom is `400 session_not_active` on turn 2, part-way through a run.
   Storing *results* elsewhere does not help: the conversation itself has to write session and
   message rows, so the contention is in the run, not the reporting.
2. **The suite writes.** `simulation._seed_claims` DELETEs every `user_identity_claims` row for
   a persona before each run. `circles_zip/live_seed.py` inserts and deletes
   `circle_affiliations`. `circles_zip/live_impl.AreaStateLive.transition_zip` recounts a ZIP
   and can permanently stamp founding. Pointed at dev, all of those quietly hit rows a teammate
   is using — and succeed.

A per-developer local stack fixes both, and adds a third thing you cannot have on dev: **full
service-role control of world state**, which is what lets `policy_eval --backend inproc` honour
a scenario's pinned world for real (see [World seeding](#5-optional-pin-a-scenarios-world)).

Because the fix is "point at local", the failure mode is "forgot to point at local". So every
seeding/destructive path now calls `local_guard.require_local()` and **refuses** against a
non-local target. See [The safety preflight](#the-safety-preflight).

---

## 0. Prerequisites

* **Docker** running (Docker Desktop on Windows/macOS).
* **Supabase CLI** — `npm i -g supabase`, `brew install supabase/tap/supabase`, or
  `npx supabase@latest <cmd>`.
* `psql`, for the sim-account seed. If you do not have it natively, the stack ships one —
  see the `docker exec` variant in step 3.

The stack is already initialised in this repo: `supabase/config.toml` (`project_id = "tagalng"`)
and 177 migrations under `supabase/migrations/`, so a local start reproduces the whole schema,
including the onion RPCs and the `zip_unlock` state machine.

## 1. Start the stack

```bash
cd <repo root>
supabase start
```

Ports come from `supabase/config.toml`:

| Service | URL |
|---|---|
| API (Kong) — this is `SUPABASE_URL` | `http://127.0.0.1:54321` |
| Postgres | `postgresql://postgres:postgres@127.0.0.1:54322/postgres` |
| Studio | `http://127.0.0.1:54323` |

`supabase start` prints the local `anon key` and `service_role key`. They are the standard
demo keys — the same on every developer's machine, safe to paste into `.env.local`, and useless
against anything but your own loopback. Reprint them any time with `supabase status`.

## 2. Apply the migrations

A fresh `supabase start` already applies everything in `supabase/migrations/`. To reset to a
known-clean schema at any point (this is also the undo button for world seeding):

```bash
supabase db reset      # drops, re-applies all 177 migrations, re-runs supabase/seed.sql
```

Verify a late migration landed:

```bash
psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
  -c "select unlock_state from public.zip_unlock limit 1;" \
  -c "\d public.capability_index"
```

## 3. Seed the six sim accounts, with a password you choose

`supabase/seed_sim_accounts.sql` creates `p1-sim … p6-sim` with **fixed UUIDs** — but note they
are NOT the UUIDs in `personas.json`: the seed uses `51000001-0001-4000-8000-000000000001` …,
while `personas.json` carries the shared dev project's ids (`3ad1c73f-…` etc.). The harness resolves
this itself — every entry point authenticates BY EMAIL, and `simulation._effective_user_id()` takes
the user id off the authenticated session rather than off `personas.json`, printing a line when the
two differ. Do not "fix" the mismatch by editing either file: seeding by the json id while
conversing as the email id puts the claims on one user and the conversation on another, which
scores a silently empty profile as though it were real, pre-confirmed, each with a `home_block_id`. It is re-runnable
and takes the password as a psql variable — nothing is committed.

```bash
export SIM_PASSWORD='pick-anything-local-only'

psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" \
  -v sim_password="$SIM_PASSWORD" \
  -f supabase/seed_sim_accounts.sql
```

No local `psql`? Use the one inside the stack's Postgres container:

```bash
docker exec -i supabase_db_tagalng \
  psql -U postgres -d postgres -v sim_password="$SIM_PASSWORD" \
  < supabase/seed_sim_accounts.sql
```

The script ends with a `select` that lists the six accounts and their `confirmed` flag — if you
see six `t` rows, you are done. Re-running it **resets** `encrypted_password`, which is the fix
if you later change `SIM_PASSWORD`.

> The file's own header notes it is dev/staging only and must never run on prod. That still
> holds; a local stack is simply the most dev-only place there is.

## 4. Point the worker and the harness at local

Two files, because they are read by two different processes.

**a) The harness** — repo-root `.env.local` (read by every simulations entry point):

```bash
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_ANON_KEY=<anon key from `supabase status`>
SUPABASE_SERVICE_ROLE_KEY=<service_role key from `supabase status`>
SIM_PASSWORD=<the password you seeded with>
LANA_BASE_URL=http://127.0.0.1:8081
OPENAI_API_KEY=sk-...
LANA_LLM_PROVIDER=openai
```

`LANA_BASE_URL` is worth setting explicitly: `simulation.py` defaults to
`http://localhost:8000`, but `scripts/run-lana-worker-local.sh` serves on **8081**.

**b) The worker** — `deploy/lana-worker.env` (gitignored; `run-lana-worker-local.sh` sources
*only* this file, never `.env.local`):

```bash
LANA_LLM_PROVIDER=openai
OPENAI_API_KEY=sk-...
SUPABASE_URL=http://127.0.0.1:54321
SUPABASE_ANON_KEY=<anon key>
SUPABASE_SERVICE_ROLE_KEY=<service_role key>
```

Then:

```bash
./scripts/run-lana-worker-local.sh
curl -s http://127.0.0.1:8081/health
```

**If the two files disagree about `SUPABASE_URL`, the run is meaningless**: the harness would
authenticate against one database and the worker would read another, and the first symptom is a
JWT the worker rejects. Check both before a long run.

Now run things as usual:

```bash
cd services/lana-worker/simulations
python runner.py --pr                       # the PR-gate bucket set
cd policy_eval && python run_eval.py --backend inproc
```

## 5. (Optional) Pin a scenario's world

Only possible locally. `local_world.py` writes the exact inputs `app/policy/world.py:105`
derives its state vocabulary from — `zip_unlock.unlock_state`, `users.home_zip`,
`users.role`, `users.grammatical_gender`, `users.locale`, the two `*_verified_at` columns, and
confirmed `circle_affiliations` (grounding a circle upserts a namespaced `places` row).

As a library, inside `policy_eval`:

```bash
SIM_ALLOW_WRITES=1 SIM_INPROC_SEED_WORLD=1 python run_eval.py --backend inproc
```

Each scenario's world is written for the account, then **read back** through the real
`world_state()`. `checks.check_world_fidelity` scores that `PASS` only if the read-back equals
the pin; a seed that silently did not take is `UNSCORED`, never a quiet pass. This is strictly
better than `SIM_INPROC_INJECT_WORLD=1`, which fabricates only `world_state()` and leaves the
goals/claims/capability reads seeing a contradictory world.

Standalone:

```bash
cd services/lana-worker/simulations
SIM_ALLOW_WRITES=1 python local_world.py --user <uuid> \
  --zip 32832 --unlock warming --role parent --gender feminine --verified \
  --circle "fitness:Life Time"

SIM_ALLOW_WRITES=1 python local_world.py --teardown --user <uuid>
```

`--teardown` removes only the `circle_affiliations` (and their `places`) this tool created —
they carry `detail = 'policy-sim-world'`. `zip_unlock` and `users` are UPDATES with no snapshot;
their undo is `supabase db reset`, which on a local stack is cheaper and more trustworthy than
bookkeeping.

## 6. Tear down

```bash
supabase stop                 # keeps the database volume; `supabase start` resumes it
supabase stop --no-backup     # discards the volume entirely
supabase db reset             # keep the stack, throw away all data + reseed from migrations
```

After a reset, re-run **step 3** — `db reset` runs `supabase/seed.sql` but *not*
`seed_sim_accounts.sql`, so the six persona accounts will be gone.

Point `.env.local` and `deploy/lana-worker.env` back at the dev project when you are done, or
leave them local: everything destructive is now refused against dev anyway.

---

## The safety preflight

`local_guard.py` classifies `SUPABASE_URL` and gates every seeding/destructive path.

| Verdict | What matches | Writes allowed? |
|---|---|---|
| `local` | `localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, `*.localhost`, `kong`, `supabase_kong_tagalng`, `host.docker.internal` — **exact** host match | yes |
| `dev` | `*.supabase.co` / `.in` / `.net` — a hosted, shared project | no |
| `unknown` | anything else: empty, a typo, a LAN IP, a tunnel, `postgres://…` | no |

Classification is **fail-closed**: only a positively recognised local host counts. A host that
merely *contains* `localhost` (`https://localhost.evil.com`) or `127.0.0.1`
(`https://127.0.0.1.nip.io`) is `unknown` and refused — both resolve off-box.

Guarded call sites:

| Path | What it would do |
|---|---|
| `simulation._seed_claims` | DELETE every `user_identity_claims` row for a persona |
| `circles_zip/live_seed.py` (`_guard_writes`) | INSERT/DELETE `circle_affiliations` by shared marker |
| `circles_zip/live_impl.AreaStateLive.transition_zip` | `recount_zip_unlock` — rewrites `zip_unlock`, can stamp founding |
| `local_world.py` (all writes) | `zip_unlock`, `users`, `circle_affiliations`, `places` |

This **layers on top of** the existing `SIM_ALLOW_WRITES=1` gate; it does not replace it. The
two answer different questions — `SIM_ALLOW_WRITES` is "I meant to write", `local_guard` is
"…to *this* database" — and where both apply, both must pass.

**Override:** `SIM_ALLOW_NONLOCAL_WRITES=1` allows a non-local target (e.g. the original
dev-seeding use case for `live_seed.py`). Deliberately a *different* variable from
`SIM_ALLOW_WRITES`, so the habitual one can never silently also mean "on the shared project".
Every overridden call prints a banner naming the operation and the target.

Proven, not assumed: `simulations/selftest.py` asserts the full classifier truth table and
drives the real guarded functions with the shared dev URL, asserting each refuses.

## Auth, and what its errors now tell you

A Supabase password grant answers `400` identically for three unrelated mistakes, and on a fresh
local stack all three are live at once. `sim_auth.py` is now the single entry point
(`simulation._jwt_for_persona` and `policy_eval/live_policy._password_grant` both call it) and
names the likely cause instead:

* pointed at the wrong project → it prints the classified target
* `SIM_PASSWORD` empty or wrong → it says so, and tells you to re-run the seed
* accounts never seeded → it gives you the `psql` check for `auth.users`

**No `SIM_PASSWORD`?** With `SUPABASE_SERVICE_ROLE_KEY` set, `sim_auth` falls back to the
service-role flow `seed_sim_accounts.sql`'s own header describes: `POST /auth/v1/admin/
generate_link` (`type=magiclink`) → redeem the returned `token_hash` at `POST /auth/v1/verify`.
Stock GoTrue admin API, no product change. Password auth stays the default where a password
exists, because the fallback needs the service-role key.

## What is **not** verified

This document's stack commands were **not executed**. The environment this was implemented in
has no Docker, no `supabase` CLI and no `psql`, so no local stack could be started:

```
$ which supabase docker psql   ->  (none)
$ docker info                  ->  command not found
```

Consequently these are written from `supabase/config.toml`, the migration files and the seed
script, and are unproven end-to-end:

* the exact `supabase start` / `db reset` output and the container name
  `supabase_db_tagalng` (derived from `project_id = "tagalng"`);
* whether `seed_sim_accounts.sql` applies cleanly on a fresh local schema (it depends on
  `handle_new_user()` having created the `public.users` rows, and on the `extensions.crypt`
  functions being present);
* the magic-link fallback end-to-end, including the response field carrying the hashed token —
  `sim_auth._hashed_token` reads both `hashed_token` and `token_hash`, at the top level and
  under `properties`, and raises with the response keys rather than guessing further. Marked
  `# GUESSED` in the source.

What **is** verified is everything that does not need a database: the classifier truth table,
the refusal of every guarded write path against the real dev URL, `local_world`'s pure
world→writes mapping, and every existing selftest in the suite (all green).
