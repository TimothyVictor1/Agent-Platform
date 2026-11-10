<!-- DRAFT — T22 (docs). Do not record from this script until the checklist at the
bottom is green. Beats are planned from SPEC §4.1; adjust to what actually ships. -->

# Demo script (2 minutes) — DRAFT

A tight walkthrough for a screen recording: one request → plan → three agents
collaborating live → the hand-off → the unified replay. Target: 2:00 of finished
footage, recorded at a 16:9 (or 16:10) window, dark theme, browser zoom at 90–100%.

## The prompt (exact, from SPEC §4.1)

```
Compare the pricing and free tiers of Notion, Obsidian, Logseq and Anytype. Build a
spreadsheet in LibreOffice Calc with one row per app, and a one-page summary
document recommending the best choice for a student.
```

**Deterministic variant (recommended for recording).** Serve the frozen fixture site
from `evals/fixtures-site/` (four fake vendors — "Alder Notes Pro", "Birch Notes Pro",
"Cedar Notes Pro", "Dune Notes Pro"; see `evals/README.md`; reachable from sandboxes
at `http://eval-fixtures`, from the host at `http://127.0.0.1:8090`) and prompt
accordingly, e.g.:

```
Compare the monthly and yearly pricing of the four note-app vendors listed at
http://eval-fixtures. Build a spreadsheet in LibreOffice Calc with one row per
vendor, and a one-paragraph summary recommending the best deal per seat.
```

The real-sites variant is livelier but slower and non-deterministic; the fixture
variant runs consistently, which matters when you re-record.

## Setup checklist (before recording)

- [ ] `make dev` works and a `local-free` session with `docker_local` sandboxes
      completes end-to-end (three packages: API T09 and runtime wiring T13, plus the
      sandbox image built with `make sandbox-image`).
- [ ] Supervisor active (T17) — the beats below show it answering a question and
      reviewing a result. Until then, drop the supervisor beats and keep the
      board/hand-off beats (which the scheduler + coordinator tools already cover).
- [ ] Approval workflow (T18) — only needed for the optional approval beat.
- [ ] `local-free` model profile healthy (Home page shows provider health badges).
- [ ] Optional: eval fixture site up (see above).
- [ ] Agents named Ada, Brook, Cleo (deterministic roster) — do not re-record just
      to chase their exact wording; capture a good 40-second live window.

## Beats

| Time | On screen | Say / point at |
|---|---|---|
| 0:00–0:10 | **Home** (`/`): paste the prompt | "One request. Pick three agents, the free local profile, and Docker desktops — everything local, nothing cloud." |
| 0:10–0:20 | Home → click **Plan**, then **Plan Review** loads | "The planner turns the request into a task DAG — research tasks first, spreadsheet depends on both, summary depends on the spreadsheet." |
| 0:20–0:30 | **Plan Review**: click a node; edit one acceptance criterion; **Approve** | "Review and edit it like a code review. Approving boots three Linux desktops." |
| 0:30–0:45 | **Live Session**: agent grid, three live desktops; agents booting → working | "Each agent has its own machine and its own browser. Ada researches Notion and Obsidian, Brook takes Logseq and Anytype." |
| 0:45–1:05 | **Board tab**: a `claim` ("I'm covering Logseq + Anytype"), a `fact` with its source URL, a `question` and the supervisor's `answer`; then the Focus view inbox showing that answer arriving in the agent's next step | "They coordinate while they work — not after. Ada asks whether prices are monthly or yearly; the supervisor answers; it lands in her very next step." |
| 1:05–1:20 | **Tasks tab**: T1/T2 turn `done`, T3 picks up on the agent whose sandbox already holds the relevant context (affinity rules); library view shows the spreadsheet published (SPEC's happy path names it `comparison.ods`), then fetched into the next agent's `Shared/` | "Files move between machines through the artifact store — the downstream agent starts with the spreadsheet already there and finishes the summary document." |
| 1:20–1:35 | *(Optional approval beat)* an approval card with screenshot; Approve with a note; the agent continues | "Anything sensitive — submits, purchases, emails — waits for me." |
| 1:35–1:55 | **Replay** (`/sessions/[id]/replay`): press play, show lanes + thumbnails, one message arrow between lanes, click a marker to inspect it | "Every event is recorded. One timeline shows all agents' screens, every message as an arrow — debuggable after the fact." |
| 1:55–2:00 | Back on Live Session final frame: artifacts list (the spreadsheet and the summary document) and the cost meter | "Real files out, full audit trail, free local stack. Thanks for watching." |

Recording notes:

- Capture the **Replay page** for the closing shot (that's what the README GIF uses).
- The live section is streamed noVNC tiles; if tile updates stutter, swap the live
  segment (0:30–1:35) for a shortened Replay playback of the same beats — visually
  similar and frame-stable.
- Keep text legible: don't scroll diagonally; zoom the board/task panel instead.
- If the session runs long, cut mid-beat rather than speeding up: use the replay
  scrub to continue at a natural moment.

## Post-production

1. Trim dead air (agent step gaps) to ~1.5×; keep step events audible-synced to any
   voiceover.
2. Overlay captions naming the concept at each beat: *Plan → DAG*, *Live board*,
   *Inbox digest*, *Artifact hand-off*, *Approval gate*, *Unified replay*.
3. Export GIF (≤ ~10 MB for the README) plus MP4 for linking.

## Do-not-ship list (draft discipline)

- Do not edit sessions, plans or timing in post to imply capabilities that were not
  exercised (no splicing a supervisor answer that did not happen into a different
  session).
- Do not show speeds/numbers that no eval report backs (costs, "6× faster" claims).
- Do not expose `.env`, API keys, tickets or the interactive stream URLs on camera.
