# The assistant

NETHBot on NETHOS: what it is, how it is installed, and the two things about
this system that make it behave differently from running it on a laptop.

## What is wired up

- **Ask**, on the panel. A field you type into, opened from the bar. It talks
  to NETHBot over the WebSocket its backend already exposes, from the page --
  the daemon is not in the path. That is deliberate: nethosd would otherwise
  need a WebSocket client and NETHBot's protocol, and an optional assistant
  must not become a dependency of the thing that draws the desktop.
- **Troubleshooter** (`apps/troubleshooter`). Reload surfaces, restart the
  daemon, restart the shell, and the diagnostics `nethos-doctor` collects --
  without a terminal, which is the whole point.
- `/api/nethbot` reports what was found; `/api/nethbot/open` starts it and
  opens its own UI in a nethos-view window, so it wears the same chrome as
  everything else.

## Installing it

NETHBot is looked for in `~/.local/share/nethbot`, `/usr/share/nethbot` and
`~/nethbot`, identified by `backend/main.py`.

    mkdir -p ~/.local/share/nethbot
    tar xzf nethbot.tgz -C ~/.local/share/nethbot

Its server dependencies come from the archive, not from pip -- the image has
neither `pip` nor `ensurepip`, and this is a Debian system underneath:

    sudo npkg fetch python3-fastapi python3-uvicorn python3-httpx

`nethbot_start()` uses `.venv/bin/python3` beside the checkout if there is one
and the system interpreter otherwise, so no virtualenv is needed.

**It runs on Linux unchanged.** Its screen-control paths (pyautogui, Quartz)
are macOS-bound but lazily imported, so `from backend.main import app`
succeeds untouched. What does not work there is clicking and screenshotting;
the shell and chat paths -- the ones a troubleshooter needs -- do.

## The model

    # server, in its own unit
    systemd-run --user --collect --unit=ollama /usr/local/bin/ollama serve
    ollama pull gemma4:e4b

`agent/llm.py` points at `http://localhost:11434/api/chat` and `gemma4:e4b`.
Ollama itself is not in Debian; install it from the release tarball
(`ollama-linux-amd64.tar.zst`, ~1.4GB, extracted to `/usr/local`) rather than
by piping the vendor's install script into a root shell.

The image is ~6G and a model is ~10G, so run `nethos-growroot` first. On a
115G disk it takes the root filesystem from 3.3G to 114G, online, in about a
second.

## Two things that are specific to this system

**It must not be a child of nethosd.** A process spawned by the daemon lives
in the daemon's cgroup, and systemd kills the cgroup when the unit restarts --
so the assistant died every time nethosd was restarted, including from the
Troubleshooter button whose purpose is to restart things when something is
wrong. It is started with `systemd-run --user --unit=nethbot`, giving it its
own unit and its own lifetime. Do not "simplify" this back to a plain spawn.

**Absence is normal and must be visible.** NETHBot is frequently not
installed. Every entry point says what it found rather than doing nothing when
pressed: the Ask field prints "Could not reach NETHBot on port 8000", and the
Troubleshooter's button says where to put it. A control that silently does
nothing is the failure mode this project produces most often.

## Not done

Repair when the desktop does not come up needs somewhere to run that is not
the thing being repaired -- A/B partitions per `docs/ABUPDATE.md`, not
anything to do with the assistant. See roadmap section 7.
