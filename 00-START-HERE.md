# Start here

This pack contains everything you need to build the platform with Superset, Claude Code and OpenCode.

## What is in the pack

| File | What it is | Where it goes |
|---|---|---|
| `SPEC.md` | The full, detailed specification of the idea. This is "the prompt": every agent reads it. | repo root |
| `TASKS.md` | 23 tasks in 6 waves, each with a copy-paste prompt, marked **CC** (Claude Code) or **OC** (OpenCode). Also the review and fix prompts. | repo root |
| `AGENTS.md` | Rules every coding agent must follow (OpenCode reads this file automatically). | repo root |
| `CLAUDE.md` | Imports AGENTS.md for Claude Code, plus a few extra notes. | repo root |
| `.superset/config.json` + `scripts/superset-*.sh` | Superset setup/teardown: copies your `.env` into each new worktree, gives each worktree its own ports and database, installs dependencies. | repo root |

## Steps: opening the pack in Superset

Superset works on a **git repository**, not on loose files. So the pack first goes into a repo, then you open that repo in Superset.

### 1. Put the pack into a git repo (once)

```bash
mkdir ~/Projects/agent-platform && cd ~/Projects/agent-platform
git init -b main
unzip ~/Downloads/multi-agent-build-pack.zip
cp -r multi-agent-build-pack/. . && rm -rf multi-agent-build-pack
chmod +x scripts/*.sh
git add -A && git commit -m "chore: add spec and agent instructions"
```

Optional but recommended: create an empty repo on GitHub and push (`git remote add origin <url>` then `git push -u origin main`), so you can review work as pull requests.

Check that `.superset/config.json` is at the repo root. Hidden folders are easy to lose when copying.

### 2. Open the repo in Superset

1. Open the Superset app.
2. Press **⌘N** (New workspace). In the dialog, choose your repository folder (`~/Projects/agent-platform`).
3. Name the workspace `t00-bootstrap`. Superset creates a git worktree and branch for it.
4. Superset runs the setup script from `.superset/config.json` automatically. On this first run it only copies `.env`, because there's no code to install yet. A terminal opens when it finishes.

### 3. Start the agent and give it the task

In the workspace terminal:

- For a **Claude Code** task, run `claude`, then paste the task's prompt from `TASKS.md` (the text inside the grey box under that task).
- For an **OpenCode** task, run `opencode`, choose your local model, then paste the prompt. Start in plan mode as described in `TASKS.md` §O.

### 4. Review and merge

- Press **⌘L** to open the Changes panel and read the diff.
- When the agent reports that all tests pass, commit and push from the Changes panel and open a pull request (or merge with `git merge` in the main checkout).
- For OpenCode branches, first run the Claude Code review (§R in `TASKS.md`) inside that workspace.
- After merging, delete the workspace with **⌘W**. Superset removes the worktree and runs the teardown script.

### 5. Next waves

- Create one workspace per task in the next wave. **⌘⇧N** quick-creates one; rename it to the task's branch name (e.g. `t01-sandbox-image`).
- Switch between running agents with **⌘1–9**.
- Keep 2–3 OpenCode and 1–2 Claude Code workspaces running at the same time.
- Before starting a new wave, make sure the previous wave is merged into `main`, so new worktrees start from the latest code.

Before your first workspace, create `.env` in the main checkout (copy `.env.example` after T00 creates it, and fill in only what you use). The setup script copies it into every new workspace. Never commit it.

## The short master prompt (if a tool asks for one prompt)

Paste this when you start any new agent session in the repo, followed by the task prompt:

```
You are building a platform where a team of AI agents does real computer work together. Each agent controls its own isolated Linux desktop (local Docker or E2B) through a screenshot → model → one action loop. Unlike side-by-side systems, agents collaborate while they work: a live structured Board (facts, claims, questions, answers, decisions, blockers), direct messages, an inbox digest injected into every step, and an Artifact Store that moves files between sandboxes. A Planner produces a task DAG with typed inputs/outputs that the user edits and approves. A Scheduler assigns each task to exactly one agent with leases, affinity and retries. An event-driven Supervisor answers questions, detects stuck agents, verifies results against acceptance criteria and re-plans. The human can watch every desktop live, steer any agent, take over a desktop, and must approve sensitive actions. Everything is event-sourced in Postgres and can be replayed as one timeline. Models and sandboxes are provider-agnostic, so the whole system runs free on local models and local Docker.

SPEC.md is the source of truth and AGENTS.md contains the rules you must follow. Read both before doing anything. Then do only the task below.
```

## Which model for the product's own agents (free options)

The coding tools and the product's agents are separate things. Claude Code and OpenCode *build* the project. The product's agents then need a vision model *at runtime* that can read screenshots and click accurately.

- **Free and unlimited: your own hardware.** Serve an open-weight vision model with good GUI grounding through llama.cpp, Ollama or vLLM, and point the `local-free` profile in `config/models.yaml` at it. Every agent step is one model call, so local is the only option with no daily cap. With several agents sharing one machine, set `concurrency` in the profile so they queue instead of overloading it.
- **Free cloud tiers have tiny daily caps.** Gemini's free tier and OpenRouter's free models work through the same OpenAI-compatible adapter, but their daily request limits run out after one or two tasks. One task can take 50–200 steps. Google also uses free-tier data for training and only allows the paid tier for apps serving users in the EU.
- **Sandboxes.** `docker_local` is free. E2B's free Hobby tier gives a one-time credit and sessions up to one hour, which is plenty for testing the cloud path.
- **Best result for the demo video.** Switch the profile to a paid Claude model for the final recording; it's a config change, not a code change.
