# NETHOS

A Linux distribution whose desktop is HTML/CSS rendered by WebKit, on top of a
Debian package archive repacked Arch-style by a Python package manager.

## Layout

    pkg/            npkg — the package manager (convert, bootstrap, elf, rpm, service)
    payload/        the desktop, installed to /usr/share/nethos
      bin/          nethos-view (hosts the WebKit surfaces), nethos-* tools
      shell/        panel, dock, launcher, desktop — shell.js is the whole shell
      apps/         one directory per app: app.json + index.html
      lib/          nethos.css (design tokens), nethos.js, nethos-ui.js (the SDK)
      nethosd/      nethosd.py — HTTP API on 127.0.0.1:7777
      hypr/ sway/   compositor config (Hyprland default, sway fallback)
    scripts/        build-image.sh (arm64), build-x86.sh, run.sh
    docs/           read these before changing anything

## Read first

- `docs/HANDOFF.md` — project state, and the debugging technique that works.
- `docs/DESIGN.md` — the design doctrine. It is enforced: anything that invents
  its own colour, radius or spacing is a bug. Rule 1 (one accent), rule 3 (radii
  nest), rule 6 (8px grid), rule 33 (dock icons) come up constantly.
- `docs/ROADMAP.md` — what is done, what is blocked, and why.

## The one thing to know

**npkg runs no Debian maintainer scripts.** Nearly every mystery bug in this
project has been a `postinst` that never ran, and each presents as a completely
unrelated symptom (no DNS, no 5GHz, dead power buttons, nobody can log in).
Before diagnosing a hardware or driver fault, check whether a post-install step
is simply missing. `docs/HANDOFF.md` has the table.

## Working here

- The desktop writes nothing to a terminal. `console.log` from any shell page
  lands in `~/.cache/sway.log`; injecting a probe into `shell.js` and running
  `nethos-reload` is the fastest way to see what the UI is doing.
- `tools/workbench.html` is a mock desktop in a browser using the **real**
  stylesheets. Visual changes can be checked there in seconds instead of a
  three-minute rebuild.
- **On macOS you cannot boot the image** — `build-image.sh` needs an arm64
  Debian VM. Verify at the code and workbench level, and say plainly in your
  final message what you could not verify.
- Measure before concluding. The frame-clock bug was found only after three
  confident wrong diagnoses.

## Work comes from GitHub issues

Open issues on `Caleb22589/nethos` are the task list. There is no separate
tracker file.

    gh issue list                       what is open
    gh issue view <n>                   the full brief
    gh issue develop <n> --checkout     branch for it

Each issue names the files it owns. Do not edit files another open issue owns —
parallel agents work in separate worktrees and overlapping edits collide on
merge. If the real fix turns out to be in someone else's file, say so in your
final message instead of reaching into it.

**Commit with `Closes #<n>` in the message.** GitHub closes the issue when the
branch merges to `main`, so what is fixed and what is not stays accurate
without anyone maintaining a list.

Commit as soon as the work stands up, not at the very end. Agents here have
been killed mid-task by API credit limits after finishing the work but before
saving it, and uncommitted work in a worktree is one `git worktree prune` from
gone.

## Conventions

- Comments explain *why*, especially where the obvious approach failed. Match
  that voice: several files record what was tried and why it did not work.
- Prose in docs and comments is British-spelled.
- Use `ui.ask` / `ui.confirm` / `ui.menu` from `lib/nethos-ui.js`. Never
  `prompt()`, `confirm()` or `alert()` — they cannot be styled and they are the
  inconsistency the SDK exists to stop.
