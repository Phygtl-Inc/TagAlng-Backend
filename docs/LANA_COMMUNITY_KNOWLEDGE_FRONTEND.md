# Community knowledge documents: frontend guide

Owners and co-owners can upload documents (a handbook, FAQ, rules, schedule) to a community. When someone chatting inside that community asks a question the documents answer, Lana answers from them and names the document it used.

The frontend is one **Knowledge** section in the community edit sheet, next to Co-owners. A first version is built in the PWA PR on branch `feat/community-knowledge-docs`; this guide is the contract it follows. The chat side needs no frontend work: answers arrive as ordinary Lana replies.

## Before you start

- The backend is behind the worker env flag `LANA_COMMUNITY_DOCS=1`. When it's off, every endpoint below returns `404 {detail: "not_enabled"}`. Hide the section on that 404.
- Migration `20270212120000_community_knowledge_docs.sql` must be applied on the database you test against.
- These are **worker endpoints**, not RPCs. Every one is a `POST`, like the rest of the worker: call them with `lanaFetch`, and use `lanaUpload` (with its `fields` argument) for the file. Validate responses with zod.
- Errors come back as `{detail: "<code>"}`.

## Who can do what

| Role | Sees the Knowledge section | Upload / delete / change visibility |
| --- | --- | --- |
| Owner or co-owner | Yes, every document with its processing status | Yes |
| Member | Optional: a read-only list of ready documents | No |
| Visitor | Optional: a read-only list of public documents | No |

`you` and `can_manage` in the list response tell you which view to render.

## API

### `POST /lana/communities/docs/list`
Body `{ "place_id": "…" }`. Loads the section.
```json
{
  "you": "manager" | "member" | "visitor",
  "can_manage": true,
  "max_docs": 25,
  "documents": [{
    "doc_id": "…", "title": "Club handbook", "summary": "…" | null,
    "visibility": "members" | "public", "updated_at": "…",
    "status": "uploaded" | "processing" | "ready" | "failed",
    "error": null | "pdf_unreadable" | "…",
    "source_mime": "application/pdf", "token_count": 4200, "version": 1, "created_at": "…"
  }]
}
```
- `status`, `error`, `source_mime`, `token_count`, `version` and `created_at` appear **only for managers**.
- Members get ready documents that are `members` or `public`. Visitors get ready `public` documents only.

### `POST /lana/communities/docs/upload` (multipart)
Fields:
- `file` (required)
- `place_id` (required)
- `title` (optional; defaults to the file name)
- `visibility` (optional, `members` or `public`; defaults to `members`)
- `replace_doc_id` (optional; uploads a new version of an existing document, keeping its id and visibility)

Returns the document row with `status: "uploaded"`. Processing runs in the background and takes from a few seconds up to about a minute for a long PDF. **Poll the list every 3 s while any row is `uploaded` or `processing`**, and stop when none are.

- Accepted files: `.md`, `.txt`, `.docx`, `.pdf`, up to 10 MB. PDFs can be up to 200 pages.
- Errors: `not_manager` (403), `type_not_allowed` (415), `file_too_large` (413), `empty_file`, `too_many_docs` (409), `invalid_visibility`, `place_not_found` (404), `sign_in_required` (403).

### `POST /lana/communities/docs/preview`
Body `{ "doc_id": "…" }`. Managers only. Returns the row plus `markdown` (the converted text Lana reads) and `sections: [{id, ord, heading_path[], covers, token_count}]`. Use it for the preview.

### `POST /lana/communities/docs/visibility`
Body `{ "doc_id": "…", "visibility": "members" | "public" }`. Returns the updated row.

### `POST /lana/communities/docs/reingest`
Body `{ "doc_id": "…" }`. Re-runs processing on the stored file. This is the **Try again** action for a `failed` row. Afterwards, poll the list as after an upload.

### `POST /lana/communities/docs/delete`
Body `{ "doc_id": "…" }`. Deletes the document, every version of the file and its sections. Returns `{ "deleted": true }`.

### `POST /lana/communities/docs/gaps`
Body `{ "place_id": "…" }`. Managers only. Returns `{ "gaps": [{ question, coverage: "partial" | "none", created_at }] }`: recent questions the documents couldn't fully answer. Not shown in the first version; it powers a future "What people asked that your docs don't cover" list.

## What the PWA has

- **`src/lib/community-docs.ts`:** the endpoint wrappers, zod schemas, `docsErrorKey()` and `docFailureKey()`.
- **`community-knowledge-section.tsx`:** the section itself, rendered in two places:
  - the owner/co-owner sheet (`community-settings-form.tsx`), with every control;
  - the member sheet (`community-edit-drawer.tsx`), as a read-only list (hidden when empty).
- **Manager view:**
  - an info line;
  - a members/anyone choice for the next upload, and **Upload document**;
  - one row per document with its title, summary, a status or visibility pill, and **Preview**, **Answer anyone** / **Members only**, and **Delete** (tap twice to confirm). A failed row shows the reason and **Try again**.
- **Preview** opens a nested drawer showing the converted text, rendered as Markdown.
- **Not built yet:** Replace file (the backend supports it through `replace_doc_id`) and the gaps list.

## Error copy

Put these under one i18n namespace (for example `communityDocs.errors.*`) in `en.json`, `es.json` and `pt-BR.json`.

| Code | Suggested message |
| --- | --- |
| `not_manager` | Only the community's owners can do that. |
| `type_not_allowed` | Upload a PDF, Word (.docx) or Markdown file. |
| `file_too_large` | That file is over 10 MB. |
| `empty_file` | That file is empty. |
| `too_many_docs` | You've reached the document limit for this community. |
| `sign_in_required` | Sign in to upload documents. |
| `place_not_found` / `doc_not_found` | We couldn't find that. |
| anything else | Something went wrong. Try again. |

Failure codes shown on a `failed` row (`error`):

| `error` | Suggested message |
| --- | --- |
| `pdf_unreadable` | This PDF couldn't be opened. It may be password-protected. |
| `pdf_too_many_pages` | This PDF is over 200 pages. |
| `docx_unreadable` | This Word file couldn't be opened. |
| `no_text_found` | We couldn't find any text in this file. |
| anything else | We couldn't read this file. Try again. |

## Edge cases

- **New uploads reach Lana within about 10 minutes.** Chat caches what a community's documents cover for 10 minutes per session, so a just-uploaded document may not be offered on the very next question.
- **Visibility is enforced on the server.** A visitor's question is answered from public documents only, even if a members-only document would have answered it.
- **Replace file keeps the same document.** If it fails to process, the previous version's sections stay live until the new one succeeds.

## Test checklist

- [ ] Owner uploads a PDF, DOCX and MD file. Each goes Processing… → ready, and Preview shows sensible text.
- [ ] A co-owner can upload, toggle visibility and delete. A member sees no manage controls.
- [ ] A 12 MB file → "That file is over 10 MB." A `.png` → the type message.
- [ ] A password-protected PDF → a failed row with the message, and Try again re-runs processing.
- [ ] In chat inside the community, a question the handbook answers gets a reply that names the handbook.
- [ ] A question the documents don't cover gets an honest "the documents don't say."
- [ ] A visitor's question about a members-only document isn't answered from it.
- [ ] Delete → the row is gone and Lana stops using that document within about 10 minutes.
- [ ] es and pt-BR strings show correctly.
