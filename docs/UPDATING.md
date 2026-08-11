# Updating NETHOS

The repository *is* the system. `nethos-update` pulls it and applies it to a
running machine — no image rebuild, no reinstall, and in the common case no
reboot and not even a logout.

## Everyday loop

```bash
nethos-update
```

That fetches the configured branch, installs the system files, restarts
`nethosd`, and reloads every open window. It takes seconds.

```bash
nethos-update --status          # what's installed vs what's available
nethos-update --ref v2          # a branch, tag or commit
nethos-update --packages        # also run the pacman step (slow)
nethos-update --local ~/nethos  # apply from a local checkout, no git remote
```

## What gets applied, and how fast

| You changed | Command | Cost |
| --- | --- | --- |
| Shell HTML/CSS/JS, an app, `nethos.css` | *nothing* — save the file | < 1s, automatic |
| Same, but you want it now | `nethos-reload` | instant |
| `nethosd` (the API itself) | `nethos-reload --daemon` | ~1s |
| The panel's own Chromium flags | `nethos-reload --shell` | ~3s |
| sway config | `swaymsg reload` | instant |
| Package set, autologin, branding | `nethos-update --packages` | minutes |
| Kernel | reboot | a reboot |

Only the last two need more than a second. **Nothing in normal development
needs a reboot.**

### Why it is instant

`nethosd` watches `/usr/share/nethos` and `~/.local/share/nethos/apps`. On any
change it bumps a generation counter and pushes an event down a server-sent
event stream. Every open surface — panel, launcher, every app window — is
subscribed via `EventSource` and reloads itself. That is the whole mechanism.

## Bootstrapping a machine built before the updater existed

A NETHOS image built before `nethos-update` shipped has neither `git` nor the
update tooling, so it needs one manual pass to pick them up:

```bash
sudo pacman -S --needed git
git clone https://github.com/Caleb22589/nethos.git ~/nethos-boot
sudo ~/nethos-boot/payload/install-nethos.sh --files-only
nethos-reload --daemon
```

After that `nethos-update` works on its own and this is never needed again.
Images built from the current repo include `git` and the tooling already.

## Pointing at your own repository

`/etc/nethos/update.conf`:

```sh
REPO_URL="https://github.com/you/nethos.git"
BRANCH="main"
```

Fork it, change that URL, and your machines track your fork. The checkout lives
in `~/.local/share/nethos/repo`.

### Private repositories

`nethos-update` runs `git` as you, so any credential helper or SSH key already
configured on the machine works. For a private repo either use an SSH remote:

```sh
REPO_URL="git@github.com:you/nethos.git"
```

…with a deploy key in `~/.ssh`, or cache an HTTPS token with
`git config --global credential.helper store`. Nothing NETHOS-specific.

## Rolling back

Builds are just commits, so:

```bash
nethos-update --ref <known-good-commit>
```

`/etc/nethos-release` records the timestamp of the applied build, and
`nethos-update --status` prints it next to what the repo is offering.

## Safety note

`nethos-update` runs `install-nethos.sh` from the repository **as root**. That
script is the thing that defines your OS, so anyone who can push to the branch
you track can run code as root on every machine tracking it. Treat push access
to this repo as root access to your machines — protect the branch, and be
deliberate about what you pull with `--ref`.
