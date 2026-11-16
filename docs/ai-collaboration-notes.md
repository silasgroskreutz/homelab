# How We Work With AI Here — Working Notes

*Living document. Silas's ideas go here; Hermes keeps it current as we build.*

The last line of the brief that started this: *"I am seeing lots of ideas of how to work
with AI and different builds, and I need to write out some thoughts of how we will handle
that."* This file is that place.

---

## The Shape Of It

The through-line across everything we have built is: **AI is a participant in the homelab,
not a chatbot bolted onto it.**

Concretely, that means three layers:

| Layer | What it is | Who owns it |
|---|---|---|
| **Infrastructure** | Flux-managed apps in k3s, backups, monitoring | Git is the source of truth |
| **Capability** | A scoped account per app so automation can *do things* | Least privilege, per app |
| **Interface** | Voice, chat, scripts — how *you* reach the capability | Whatever feels natural |

The middle layer is the one that unlocks the top and bottom. Without a scoped account in
Navidrome, "play my JRPG playlist" stays a fantasy. With one, it's a two-line API call.

---

## What Is Already True

**Infrastructure (working)**
- BookOrbit deployed via Flux with a local Helm chart, exposed through a Cloudflare tunnel.
- `verify-flux-app.sh` — the reusable health gate that catches the five ways an app looks
  healthy while being broken.
- `new_app.py` — scaffolds a new app (namespace, deployment, service, PVC, ingress,
  tunnel, SOPS-encrypted secret) and validates it before it can break the tree.
- `tools/backup/backup.py` — the staged-backup engine, committed but **not yet scheduled**.

**Capability (working)**
- BookOrbit: `hermes_ops` + scoped API key, non-admin, expires 2027-02-14.
- Navidrome: `hermes_ops`, non-admin, library 1, verified full playlist lifecycle —
  create → add tracks → delete. Proven with a real test playlist, then cleaned up.
- Credentials in `~/.hermes/credentials/`, mode 600, never in Git.

**Known and deliberate**
- Mealie and Audiobookshelf are **out of scope** for administration. They were K8s
  learning exercises and may be replaced. No accounts created for them, no backups
  configured, no work queued.

---

## The Automation Contract

The rules that keep automation useful without it becoming a liability.

**Automation may:**
- Read app state; report on it
- Create and manage playlists, tags, bookmarks, notes
- Run backups, verify them, prune old ones
- Provision new apps from templates

**Automation must not:**
- Touch your money (KDP publishing, any paid service)
- Touch your reputation (outbound email to other people)
- Delete library content, accounts, or credentials without asking
- Change its own permissions or create new superusers

**Escalate to you when:**
- The action is destructive and not reversible
- The action is outward-facing (published, sent, purchased)
- The action changes permissions or access

---

## Deployment Becomes A Template

The goal you named: *make deploying apps to the cluster as easy as possible.*

**Now:**

```bash
python3 tools/scaffold/new_app.py --name myapp --image ghcr.io/org/myapp:1.0 --port 8080
```

Emits the full standard shape, auto-encrypts the Secret with SOPS, registers the app in
the staging kustomization, and validates that the whole tree still renders. Then:

1. DNS A record for the hostname
2. `git commit && git push` — Flux applies it
3. `verify-flux-app.sh` proves it's actually healthy
4. Create the `hermes_ops` account for it

**Next:** a `scaffold/presets/` directory with per-app starting points for the apps you
actually want, each pre-filled with the port, storage size, and env vars that app wants —
so it becomes `new_app.py --preset kavita` instead of remembering the details.

---

## The Voice Scenario, Concretely

*"Play JRPG playlist."*

What that needs, end to end:

1. **Wake word / speech-to-text** — where does the audio come from?
   A phone, a laptop, a smart speaker, a Pi in the living room.
2. **Intent routing** — the phrase becomes an action. This is where "AI" enters, and it
   can be as simple as pattern matching a small vocabulary of commands.
3. **The action** — call Navidrome's API to find or build the playlist, then hand it to
   a player.
4. **Playback** — Navidrome is a *server*, not a speaker. Something has to actually make
   sound: Sonos, AirPlay, Chromecast, a browser tab, or a local player on Prodesk.

**What exists:** step 3 is proven. Navidrome has 71 genres including `Video Game Music`,
`Game Soundtrack`, `OST Score`, and several more, and its playlist schema supports
**smart playlists** (the `rules`/`evaluatedAt` fields are in the API response) — so
"JRPG playlist" can be a *rule*, not a hand-built list, and it stays current as the
library changes.

**What's missing:** steps 1, 2, and 4 — the whole path from microphone to sound. This is
the genuinely interesting part and it has several viable architectures.

### Three ways the voice layer could work

**A. Home Assistant + LLM** (most capable)
Home Assistant owns the voice pipeline and the speakers. A local or cheap LLM parses
intent. HA calls a small webhook that Hermes exposes.
*Pro:* best speaker support, mature voice stack, works when the internet is down.
*Con:* another system to run and maintain.

**B. Hermes as the brain, HA as the voice** (leanest)
Home Assistant's Assist handles wake word and STT, but forwards recognized *text* to
Hermes. Hermes maps it to an action and calls Navidrome directly.
*Pro:* all the intelligence in one place; easy to extend to other apps; the voice layer
stays dumb and reliable.
*Con:* Hermes is the single point of failure for anything non-voice.

**C. Direct, no assistant** (fastest to prove)
A tiny always-on service (on Prodesk or Virgil) does wake word → STT → a fixed command
grammar → Navidrome API → playback. No LLM in the loop for the common cases.
*Pro:* instant, no hallucination risk, works offline, dead simple.
*Con:* every new command is code, not a prompt.

**A defensible path: C first, then B, then A.** C proves the plumbing end to end with
one command. B makes the vocabulary natural. A is there when you want to talk to the
house about more than music.

---

## Open Questions

*Silas's call. Pick or edit.*

1. **Where does the microphone live?** Existing phone/laptop, or buy a dedicated device?
2. **What plays the audio?** Sonos/AirPlay/Chromecast if present, or a local player on
   Prodesk driving the speakers?
3. **Offline or always-online?** Option C works offline; A and B mostly need the LLM
   reachable (local Ollama on Virgil, or a paid API).
4. **Is Hermes allowed to act without confirming?** Playing a playlist = harmless.
   Deleting things = confirm first. Where exactly is that line?
5. **Which model does intent parsing?** Local on Virgil (free, private, slower) or a paid
   API (fast, cheap, but your prompts leave the house)?
6. **How far does this generalize?** "Play X" is one app. Should the same identity
   pattern cover lights, thermostats, and camera clips later?

---

## The Backlog, Honestly Ranked

**Ready to build**
- [ ] Per-app backup CronJobs through Flux — engine is committed, nothing schedules it yet
- [ ] `scaffold/presets/` — one-command installs for the apps you actually want
- [ ] Restore-test rotation — a backup is not a backup until it's restored
- [ ] Service accounts for the remaining apps you keep (linkding, komga, homarr)

**Needs a decision first**
- [ ] Voice architecture: A, B, or C
- [ ] Where automation's "ask first" line sits
- [ ] Whether Mealie/Audiobookshelf get replaced, or merely left alone

**Deliberately not doing**
- [ ] Administering Mealie or Audiobookshelf
- [ ] Anything that spends money or publishes under your name
