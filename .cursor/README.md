# TagAlng Cursor context

## What was added

| Item | Purpose |
|------|---------|
| `.cursor/rules/tagalng.mdc` | **Always on** in this repo — short pointer so every chat knows you're building TagAlng backend |
| `.cursor/skills/tagalng-backend/` | Full backend skill — stack, phases, schema sketch, fast-build checklist |

## How to use

1. **Automatic (this repo):** Open `TagAlng` in Cursor — the rule applies every chat.
2. **Explicit:** In any chat, say *"use tagalng-backend skill"* or `@tagalng-backend` if your Cursor build supports skill mentions.
3. **New chats:** You do **not** need to re-paste the PDFs; point the agent at a ticket (e.g. "implement P1 waitlist") and it should load skill + rule.

## Cross-chat memory

- **Rules + project skills** = persistent for this workspace.
- **They are not global** across unrelated folders — copy `.cursor/` to another clone or add a personal skill under `~/.cursor/skills/` if you work from multiple paths.

### Optional: personal skill (all projects)

```bash
cp -r .cursor/skills/tagalng-backend ~/.cursor/skills/tagalng-backend
```

## Docs in repo

- `Tagalong-CTO-Spec.pdf` — what to ship Phase 1→3  
- `TagAlng-RD-Kickoff.pdf` — vision, metrics, principles  
- `tagalong-agentic-spec.pdf` — post-MVP R&D only  
- `Tagalong-Architecture-Diagram.svg` — system diagram  

## Next step

Start with **EPIC P1-DB** in `.cursor/skills/tagalng-backend/tickets.md`.
