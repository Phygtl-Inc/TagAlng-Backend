# Scheduling the swarm

`swarm-nightly.yml` belongs at **`.github/workflows/swarm-nightly.yml`**.

It is parked here because the token that opened this PR lacks the GitHub
`workflow` OAuth scope, so it could not create a file under `.github/workflows/`:

```
! [remote rejected] refusing to allow an OAuth App to create or update workflow
  `.github/workflows/swarm-nightly.yml` without `workflow` scope
```

**A human with `workflow` scope must move it:**

```bash
mkdir -p .github/workflows
git mv tools/swarm/deploy/swarm-nightly.yml .github/workflows/swarm-nightly.yml
git commit -m "ci · schedule the nightly swarm run"
```

Nothing else references this path, so the move is the only step.

## Secrets the workflow needs

| Secret | What |
|---|---|
| `SUPABASE_URL` | `https://kmetmatfxdkrialwrnzj.supabase.co` (tagalng-prod) |
| `SUPABASE_SERVICE_ROLE_KEY` | the harness writes `simulations` and `swarm_run_actors` as service_role |
| `WORKER_BASE_URL` | the Cloud Run worker URL |
| `SIM_EMAIL_DOMAIN` | catch-all domain for `lana-sim+...` — only P0 needs it |
| `FIXTURES_REPO_TOKEN` | read access to the design repo holding `personas.json` and the registry |

⚠️ The production secrets pasted into the originating chat session still need
rotating (`HANDOVER_CLAUDE_CODE.md` §8.6). Do not populate these from that
session's values without rotating first.
