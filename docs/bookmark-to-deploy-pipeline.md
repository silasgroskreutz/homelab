# The Bookmark → Deploy Pipeline

*Working plan. Silas's idea, captured. Mechanics verified against the live instances on
2026-11-16. Nothing here is built yet.*

---

## The Idea, In Silas's Words

> I see lots of ideas for apps that seem useful, and I capture them in bookmarks, but then
> they just sit there. We set up Karakeep — what if you set up a service/admin account so
> you have access? You can look at recent bookmarks there. Then we need to set up a flow
> based on source(?), the tags(?) for you to take actions based on them. For example, if I
> linked to a GitHub repository, you flag it, and we would need a kanban or priority board
> (LAN access only) for deploying, preferably overnight, and then track progress. After it
> is up and running, we track setting up metadata backups, or other features within that
> application. Some GitHub projects would be near-term and want to schedule soon, some
> would be marked *someday* (due to needing hardware or other components), some would be
> archived for "as needed" — I think it is a good idea, I just do not have a use for it yet.
> Other items tagged in Karakeep, we would need to figure out how to handle, and I have not
> thought that all through yet.

**The shape:** bookmarks are the intake queue → a filter decides what's actionable → a
board tracks it → the deploy happens unattended → the finished app gets its own
follow-on lifecycle (backups, service account, monitoring).

**The real problem it solves:** not "deploy apps." It's that good ideas currently die in a
bookmark list. 246 bookmarks, and the ones worth acting on are indistinguishable from a
tweet about AWS.

---

## What I Verified (live, 2026-11-16)

Everything below was probed against the running systems, not read from docs alone.

### Karakeep — `192.168.0.105:3000`, v0.32.0

| Fact | Value |
|---|---|
| Bookmarks | 246 (233 from browser extension, 1 mobile, 12 no source) |
| Tags | 1,604 tag assignments; ~30 distinct in heavy use |
| Lists | **0** — none created yet |
| API keys | 2, both `fullaccess`, both Silas's (browser ext + mobile) |
| Users | 1 — Silas, `admin` |
| Top tags | Social Media 85, "for Kelly" 77, pokemon 63, X Platform 41, cosplay 31 |
| Domain mix | **x/twitter 236, youtube 4, github.com 2**, other 4 |
| Rule engine | tables exist, **0 rules configured** |
| Webhooks | table exists, **0 configured** |
| AI tagging | active via Ollama `qwen3:8b` (`INFERENCE_TEXT_MODEL` set) |

**The finding that matters most:** the corpus is 96% X/Twitter links. Only **2** GitHub
repos. So "if I linked to a GitHub repository" describes the *desired* future intake
pattern, not the current corpus. Whatever filter we build should be **source-based and
retroactive** — it has to work on what's already saved, not just on new bookmarks.

### Karakeep API capabilities (v0.32.0, 35 REST paths)

- **Bearer auth**, keys from Settings → API Keys.
- **Per-resource scopes exist** — this is the good news for least privilege:
  `bookmarks`, `lists`, `tags`, `feeds`, `rules`, `webhooks`, `users`, `backups`, … each
  at `read` or `readwrite`. Plus `fullaccess`.
- **Smart lists** — a list backed by a search query, auto-populating.
- **Collaborative lists** — `listCollaborators` and `listInvitations` tables exist in
  v0.32.0. Roles: `owner` / `editor` / `viewer` / `public`. Sharing requires inviting by
  **email**, and the invitee accepts. Note: Karakeep's invite flow wants SMTP; this
  instance has **no SMTP configured**, so invitations may not be deliverable as-is.
  Admin can share without interaction per upstream notes, but that needs UI verification.
- **Rule engine** — events (`bookmarkAdded`, `tagAdded`, `addedToList`, `favourited`,
  `archived`) → conditions (`urlContains`, `hasTag`, …) → actions (`addTag`,
  `addToList`, `favouriteBookmark`, `archiveBookmark`). **This is the native
  "flow based on source and tags"** — no Hermes code needed for the intake half.
- **Webhooks** — events `created` / `edited` / `crawled` / `ai tagged` / `deleted`, POSTed
  with `{jobId, bookmarkId, userId, url, type, operation}`. Optional bearer token.
  **This is the push trigger** — no polling loop needed.

### Hermes Kanban — `~/.hermes/kanban.db`

**This is the board Silas wants. It already exists.**

- 3 boards today: `default` (empty), `calibre-web` (ready=4), `systems` (blocked=8).
- Columns: `triage` → `todo` → `ready` → `running` → `blocked` → `done` → `archived`.
- **Scheduling:** `--scheduled-at "2026-06-01T03:00:00Z"` delays dispatch. That's the
  "overnight" requirement, natively.
- **Dependencies:** `link`/`unlink` parent→child. That's "after it is up and running,
  track setting up metadata backups" — a child card that unblocks on parent completion.
- **Dispatcher:** `hermes kanban daemon --interval 60`, or already running in the gateway
  (`kanban.dispatch_in_gateway: true`, 60s interval). Gateway is up on 8644.
- **GUI: the dashboard plugin is bundled** at
  `~/.hermes/hermes-agent/plugins/kanban/dashboard/manifest.json` but is **not enabled** in
  `config.yaml`'s `plugins.enabled` list. Enabling it adds a "Kanban" tab to the
  dashboard at `:9119` — drag-drop columns, live WebSocket updates, comment threads,
  dependency editor, per-profile lanes.
- **Security caveat:** the dashboard's auth middleware **explicitly skips `/api/plugins/`**.
  Plugin routes are unauthenticated by design because the dashboard binds to localhost.
  To make it LAN-reachable we bind `0.0.0.0`, which means **the Kanban REST surface
  becomes network-reachable unauthenticated** — task bodies, comments, workspace paths,
  and card create/reassign/archive. Upstream says plainly: *"Don't do that on a shared
  host."* A home LAN is not a shared host, but this needs a deliberate decision, not an
  accident. See Open Questions #1.

### Cluster

- Deploy path already exists: `tools/scaffold/new_app.py` → Flux → `verify-flux-app.sh`.
- Backup engine committed at `tools/backup/backup.py` but **not yet scheduled**.
- Homelab repo at `~/homelab`, `main` branch, Flux is source of truth.

---

## The Proposed Flow

```
  ┌─ Karakeep ──────────────────────────────────────────────┐
  │  bookmark added / tagged / added to a list               │
  │            │                                             │
  │            ├── rule engine (native, in Karakeep)         │
  │            │     github.com? → tag "deploy-candidate"    │
  │            │     + add to list "Deploy Queue"            │
  │            │                                             │
  │            └── webhook (native, in Karakeep)              │
  │                  POST {bookmarkId, url, operation}        │
  └──────────────────────┬───────────────────────────────────┘
                         │
                         ▼
              ┌─ Hermes triager ──────────────┐
              │  reads Deploy Queue           │
              │  classifies:                   │
              │    near-term  → ready          │
              │    someday    → blocked        │
              │    as-needed  → archived       │
              │  creates kanban card            │
              └──────────────┬─────────────────┘
                             │
                             ▼
              ┌─ Kanban board ─────────────────┐
              │  triage → todo → ready →       │
              │  running → done                │
              │                                 │
              │  --scheduled-at 02:00          │
              │  → overnight deploy             │
              │                                 │
              │  child cards:                   │
              │   "backup + metadata"           │
              │   "service account"             │
              │   "monitoring"                  │
              └────────────────────────────────┘
```

**Why the rule engine + webhook live in Karakeep, not in Hermes:**

Your rule "if GitHub link → flag it" is expressible *natively* — Karakeep evaluates it
the moment the bookmark lands, whether or not Hermes is running. The webhook pushes the
event to Hermes. That's less code and no polling.

**Why Kanban and not a new app:** it already has states, dependencies, scheduling,
dispatch to worker profiles, and a GUI. Silas asked for "a kanban or priority board." It
exists. Using it beats adding Vikunja/Wekan and then maintaining two boards.

**Mapping your states onto Kanban columns:**

| Your word | Kanban | Hermes behavior |
|---|---|---|
| near-term, schedule soon | `ready` + `--scheduled-at` | dispatch overnight |
| someday (needs hardware) | `blocked` | sit, with a reason in the body |
| as-needed / no use yet | `archived` | never dispatched, kept for later |
| (new) intake | `triage` | needs a decision before anything runs |

**Note the gap:** `archived` is a *terminal* Kanban column — archived cards don't
dispatch. Good for "as needed." But "someday" maps to `blocked`, which is also what
genuinely-failed deploys map to. If we want someday-items to be visibly distinct from
broken things, use a **tenant** or **tag** to separate them, not a column. Open
Question #3.

---

## Open Questions — Silas's Call

1. **How should the board be exposed on the LAN?**
   - Bind `0.0.0.0` → anyone on the LAN gets full read/write on the board, no auth
     (plugin routes bypass it). Upstream explicitly warns against this.
   - Keep it localhost-only and reach it over **Tailscale** (already on the box,
     `100.66.110.98`) → same auth gap, but only across your own devices.
   - Put the board behind Cloudflare Access / an authenticating proxy → real auth,
     more moving parts.
   - *This is a security decision, not a technical one. I won't pick it for you.*

2. **Which state lives where — Karakeep or Kanban?**
   Karakeep's smart lists and tags are the intake filter. But once something is on the
   board, Kanban owns it. Do you want Karakeep to remain the source of truth for
   "near-term vs someday", or should the Kanban card be the source of truth once created?
   (If Hermes moves a card to `blocked`, does Karakeep learn about it?)

3. **How do we distinguish "someday" from "broken"?** Separate column, tenant, or tag?
   (Kanban has no `someday` column.)

4. **Should Hermes create cards automatically, or draft them for approval?**
   Auto-create on a webhook means a bookmark can put a card on the board unattended.
   Draft-approve means a human sees the flag first. Given "flag it" in your words, I read
   this as *flag for human review* — but confirm.

5. **How aggressive is the overnight deploy?**
   - Deploy fully unattended, then report.
   - Deploy everything except the final "enable it in production" step.
   - Never deploy unattended — only *prepare* overnight, deploy when you say so.
   This one has real blast radius on a live homelab.

6. **What happens to non-GitHub bookmarks?** (You said this one's not thought through —
   flagging it as deliberately open, not as an oversight.) The corpus is 96% X/Twitter,
   so this is likely the *majority* case, not the edge case.

7. **Which GitHub repos are actually deployable to a k3s cluster?** Most are libraries,
   desktop apps, or services with their own auth model. Do we filter on a marker
   (a `deploy.yaml`? a topic tag? a curated list in Karakeep?) so we don't try to
   k8s-ify a CSS framework.

---

## Phasing

Ordered so each step is useful on its own and the risky parts come last.

**Phase 1 — Karakeep access (read-only, no automation).**
Give Hermes a scoped API key: `bookmarks:read`, `lists:read`, `tags:read`. No write.
First question it answers: *what is actually in here?* — a real inventory of the 246
bookmarks, which we don't have. Silas decides the key in the UI (2 clicks); Hermes stores
it at `~/.hermes/credentials/karakeep-service.txt` (mode 600).

**Phase 2 — the rule engine, still no Hermes involvement.**
Create the tag/list, write the rule, watch it work on a test bookmark. Now the intake
filter is live and native. Karakeep does the work; no code from us yet.

**Phase 3 — enable the Kanban GUI.**
Flip the plugin on, confirm the board renders at `:9119`, decide the exposure question
(#1) with eyes open.

**Phase 4 — the triager.**
Hermes reads the Deploy Queue, classifies into near-term/someday/as-needed, creates
cards. Draft-first per Question #4.

**Phase 5 — overnight dispatch + child cards.**
`--scheduled-at` for the deploy, dependency-linked child cards for backup / service
account / monitoring. This is where the "after it is up and running, we track setting up
metadata backups" part of your idea becomes real.

**Phase 6 — the other tags.**
Only after the GitHub path is proven. The corpus says this is where the actual volume
is.

---

## Explicitly Not Decided

- Whether the rule engine or Hermes does the source/tag filtering (both work; rule
  engine is native and survives Hermes being down).
- Whether deploys are fully unattended.
- The LAN exposure model.
- Everything about the non-GitHub majority of the corpus.
