#!/bin/bash
# Turn a stock Arch Linux x86_64 system into NETHOS — and keep it up to date.
#
#   install-nethos.sh                 full install (packages + user + files)
#   install-nethos.sh --files-only    just the NETHOS files (what updates use)
#   install-nethos.sh --no-packages   everything except the pacman step
#
# --files-only is the fast path: it skips pacman and user creation entirely, so
# applying a change from git takes seconds instead of minutes.
# --no-packages is for the live ISO and the disk installer, where the packages
# are already present and only the NETHOS layer has to be applied. Run as root.
set -euo pipefail

PAYLOAD="$(cd "$(dirname "$0")" && pwd)"
NETH_USER="${NETH_USER:-neth}"
NETH_HOME="/home/${NETH_USER}"
PREFIX=/usr/share/nethos

FILES_ONLY=0
DO_PACKAGES=1
for arg in "$@"; do
    case "$arg" in
        --files-only)  FILES_ONLY=1; DO_PACKAGES=0 ;;
        --no-packages) DO_PACKAGES=0 ;;
        -h|--help)     sed -n '2,12p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *) echo "unknown option: $arg" >&2; exit 2 ;;
    esac
done

log() { printf '\n\033[1;36m[nethos]\033[0m %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || { echo "must run as root" >&2; exit 1; }

# If the user does not exist yet we cannot do a files-only install.
if [ "$FILES_ONLY" -eq 1 ] && ! id "$NETH_USER" >/dev/null 2>&1; then
    echo "user $NETH_USER does not exist — run a full install first" >&2
    exit 1
fi

# --------------------------------------------------------------------------
if [ "$DO_PACKAGES" -eq 1 ]; then
    log "Refreshing packages (this is the slow part under emulation)"
    pacman-key --init >/dev/null 2>&1 || true
    pacman-key --populate archlinux >/dev/null 2>&1 || true
    pacman -Sy --noconfirm archlinux-keyring
    pacman -Su --noconfirm

    log "Installing the NETHOS package set"
    # swaynag ships inside the sway package; no separate dependency needed.
    pacman -S --noconfirm --needed \
        hyprland \
        xdg-desktop-portal-hyprland \
        sway swaybg swayidle swaylock \
        gtk4-layer-shell \
        webkitgtk-6.0 \
        python-gobject \
        python-dbus \
        brightnessctl \
        wireplumber \
        chromium \
        foot \
        xorg-xwayland \
        wl-clipboard \
        polkit \
        mesa \
        python \
        curl \
        git \
        networkmanager \
        wpa_supplicant \
        iwd \
        usbmuxd \
        hicolor-icon-theme \
        adwaita-icon-theme \
        papirus-icon-theme \
        ttf-dejavu \
        ttf-jetbrains-mono \
        noto-fonts \
        noto-fonts-emoji \
        xdg-utils \
        thunar \
        mousepad \
        imv \
        htop
fi

if [ "$FILES_ONLY" -eq 0 ]; then
    log "Creating the ${NETH_USER} user"
    if ! id "$NETH_USER" >/dev/null 2>&1; then
        useradd -m -G wheel,video,input,audio -s /bin/bash "$NETH_USER"
        echo "${NETH_USER}:nethos" | chpasswd
    fi
    sed -i 's/^# %wheel ALL=(ALL:ALL) ALL/%wheel ALL=(ALL:ALL) ALL/' /etc/sudoers
fi

# --------------------------------------------------------------------------
log "Installing the shell, SDK and apps"
# --------------------------------------------------------------------------
install -d "$PREFIX/shell" "$PREFIX/lib" "$PREFIX/apps"
install -m 0644 "$PAYLOAD"/shell/* "$PREFIX/shell/"
# Files only. lib/ holds the fonts directory as well now, and `install` on a
# directory fails -- under set -e that aborts the whole install, and what you
# see is the shell not updating rather than a font not copying.
for f in "$PAYLOAD"/lib/*; do
    [ -f "$f" ] && install -m 0644 "$f" "$PREFIX/lib/"
done

# The system font, installed where fontconfig looks rather than only where
# WebKit does. The shell could load it with @font-face alone, but then GTK
# applications -- Thunar, foot, every dialog npkg puts on screen -- would keep
# their own default and the system would be two fonts pretending to be one.
if [ -d "$PAYLOAD/lib/fonts" ]; then
    # Twice, to two different consumers. This copy is what the @font-face rule
    # in nethos.css fetches over HTTP -- nethosd serves /lib from here, and the
    # loop above copies files only, so without this the stylesheet asks for a
    # font that was installed somewhere it cannot see and gets a 404.
    install -d "$PREFIX/lib/fonts"
    install -m 0644 "$PAYLOAD"/lib/fonts/* "$PREFIX/lib/fonts/"
    # And this copy is the one fontconfig indexes, for applications that are
    # not a web page.
    install -d /usr/share/fonts/nethos
    install -m 0644 "$PAYLOAD"/lib/fonts/*.ttf /usr/share/fonts/nethos/
    install -m 0644 "$PAYLOAD"/lib/fonts/OFL.txt /usr/share/fonts/nethos/ 2>/dev/null || true
    # Without this the file is on disk and no application can find it by name.
    command -v fc-cache >/dev/null && fc-cache -f /usr/share/fonts/nethos >/dev/null 2>&1 || true
fi

# Apps are directories; mirror them wholesale so removed files disappear too.
rm -rf "$PREFIX/apps"
install -d "$PREFIX/apps"
cp -R "$PAYLOAD"/apps/. "$PREFIX/apps/"
chmod -R u=rwX,go=rX "$PREFIX/apps"

install -m 0755 "$PAYLOAD"/nethosd/nethosd.py /usr/bin/nethosd
# Everything in bin/, rather than a list. The list was six of the twelve tools
# and had quietly gone stale: nethos-view was not on it, so the host that draws
# every surface in the system could not be updated without rebuilding the
# image -- a fix to it installed cleanly, reported success, and changed
# nothing. nethos-snapshot was missing too, which nethos-update calls to make
# the rollback it promises. A list that has to be edited whenever a file is
# added is a list that will be wrong again.
for tool in "$PAYLOAD"/bin/nethos-*; do
    [ -f "$tool" ] || continue
    install -m 0755 "$tool" "/usr/bin/$(basename "$tool")"
done

install -d /etc/sway/config.d
install -m 0644 "$PAYLOAD"/sway/config /etc/sway/config

install -d /etc/nethos
install -m 0644 "$PAYLOAD"/hypr/hyprland.conf /etc/nethos/hyprland.conf

# Portal backends, named rather than discovered. See the file: discovery finds
# no backend for most interfaces on this desktop and spends 50 seconds finding
# that out, which is the white screen at startup.
# PipeWire, enabled rather than merely installed.
#
# It was present and inactive, which costs more than sound: the wlroots portal
# builds its ScreenCast on PipeWire, and without it the attempt fails only
# after a 25 second D-Bus timeout. That timeout is most of the white screen at
# startup -- measured, the portal took 50.2s with PipeWire down and 22.4s with
# it up. The desktop waits behind it either way.
#
# --global, because these are user units and the installer is root: enabling
# them per-user would only ever fix the account that happened to run this.
systemctl --global enable pipewire.socket pipewire-pulse.socket wireplumber.service >/dev/null 2>&1 || true
systemctl --global enable pipewire.service >/dev/null 2>&1 || true

# Kernel parameters. Never overwritten: this is a file people edit, and an
# update that reset it would silently undo a machine-specific fix.
if [ -f "$PAYLOAD/nethos/cmdline" ] && [ ! -f /etc/nethos/cmdline ]; then
    install -d /etc/nethos
    install -m 0644 "$PAYLOAD"/nethos/cmdline /etc/nethos/cmdline
fi

# A/B: the bootloader entries and the counter, installed only where the disk
# actually has two slots. On a single-slot install these would generate menu
# entries for a partition that does not exist.
if [ -f /etc/nethos/slots.conf ] && grep -q '^layout=ab' /etc/nethos/slots.conf 2>/dev/null; then
    install -m 0755 "$PAYLOAD"/grub/09_nethos_ab /etc/grub.d/09_nethos_ab
    install -m 0644 "$PAYLOAD"/systemd/nethos-ab-markgood.service \
        /etc/systemd/system/nethos-ab-markgood.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable nethos-ab-markgood.service >/dev/null 2>&1 || true
    command -v grub-mkconfig >/dev/null 2>&1 && grub-mkconfig -o /boot/grub/grub.cfg >/dev/null 2>&1 || true
fi

# Point npkg at the NETHOS repository.
#
# Without this a machine has nowhere to install anything from: npkg says "no
# repositories are configured, so there is nowhere to find <name>". The
# repository is Debian packages already converted to npkg format, published
# once, so that no machine ever repeats that work -- which on a two-core 2012
# laptop is the difference between a download and the better part of an hour.
# One place. Overridable at install time so a mirror or a local repository can
# be used without editing the installed system afterwards.
NETHOS_REPO_URL="${NETHOS_REPO_URL:-https://moddl.app}"
install -d -m 0755 /etc/npkg
if [ ! -s /etc/npkg/repos.json ]; then
    cat > /etc/npkg/repos.json <<REPOS
{
  "repos": [
    {
      "name": "nethos",
      "url": "$NETHOS_REPO_URL"
    }
  ]
}
REPOS
    log "configured the nethos package repository: $NETHOS_REPO_URL"
fi

# GSettings schemas, compiled by libglib2.0-0's postinst, which never runs.
#
# They ship as .gschema.xml and are useless until compiled into a single
# gschemas.compiled. GLib treats the absence as fatal -- "No GSettings schemas
# are installed on the system" -- and kills the process, so every WebKit view
# dies at startup and reports it as "WebKit encountered an internal error.
# This is a WebKit bug", which it is not.
#
# The image build does this in its chroot; an online install builds its root
# from the archive and never did, so an installed system came up with a
# desktop that could not draw a single window.
if command -v glib-compile-schemas >/dev/null 2>&1 && \
   [ -d /usr/share/glib-2.0/schemas ]; then
    glib-compile-schemas /usr/share/glib-2.0/schemas >/dev/null 2>&1 || true
    if [ -f /usr/share/glib-2.0/schemas/gschemas.compiled ]; then
        log "compiled GSettings schemas ($(wc -c < /usr/share/glib-2.0/schemas/gschemas.compiled) bytes)"
    else
        log "WARNING: GSettings schemas did not compile; the shell will not start"
    fi
fi

# Font caches, from fontconfig's postinst. Without them GTK falls back to
# whatever it can find and the shell renders in the wrong face.
if command -v fc-cache >/dev/null 2>&1; then
    fc-cache -f >/dev/null 2>&1 || true
fi

# ca-certificates builds its trust store in a postinst too, and without it
# every https client reports zero trusted certificates.
if [ -d /usr/share/ca-certificates ] && [ ! -s /etc/ssl/certs/ca-certificates.crt ]; then
    install -d -m 0755 /etc/ssl/certs
    find /usr/share/ca-certificates -name "*.crt" | sort | xargs cat \
        > /etc/ssl/certs/ca-certificates.crt 2>/dev/null || true
    ( cd /usr/share/ca-certificates && find . -name "*.crt" | sed 's|^\./||' | sort ) \
        > /etc/ca-certificates.conf 2>/dev/null || true
    log "built the CA trust store ($(grep -c 'BEGIN CERTIFICATE' /etc/ssl/certs/ca-certificates.crt 2>/dev/null || echo 0) certificates)"
fi

# openssh-server ships no sshd_config; its postinst writes one.
#
# npkg runs no maintainer scripts, so sshd starts, says
# "/etc/ssh/sshd_config: No such file or directory", and systemd restarts it
# five times before giving up -- on a machine where the only way in was ssh.
# Host keys come from the same postinst and are missing for the same reason.
if [ -x /usr/sbin/sshd ] && [ ! -f /etc/ssh/sshd_config ]; then
    install -d -m 0755 /etc/ssh /etc/ssh/sshd_config.d
    cat > /etc/ssh/sshd_config <<'SSHD'
# Debian's default, minus what its postinst would have templated.
Include /etc/ssh/sshd_config.d/*.conf
PermitRootLogin prohibit-password
# Password login is on: NETHOS ships a known default account, so change the
# password before putting a machine on a network anyone else can reach.
PasswordAuthentication yes
KbdInteractiveAuthentication no
UsePAM yes
X11Forwarding no
PrintMotd no
AcceptEnv LANG LC_*
Subsystem sftp /usr/lib/openssh/sftp-server
SSHD
    chmod 0644 /etc/ssh/sshd_config
    log "wrote /etc/ssh/sshd_config (openssh-server ships none)"
fi
# Host keys, likewise created by a postinst that never runs.
if [ -x /usr/bin/ssh-keygen ] && [ ! -f /etc/ssh/ssh_host_ed25519_key ]; then
    ssh-keygen -A >/dev/null 2>&1 && log "generated ssh host keys"
fi

# The kernel only ever looks in /lib/firmware.
#
# Debian ships firmware to /usr/lib/firmware and gets away with it because
# /lib is a symlink to usr/lib. npkg makes /lib a real directory to hold
# /lib/modules, so the two are different places and the kernel sees no
# firmware at all -- amdgpu refuses to load and falls back to software
# rendering, and every wifi chip comes up dead. Nothing reports it as a
# missing firmware tree; it looks like broken hardware support.
#
# The kernel's search path is compiled in as /lib/firmware/updates and
# /lib/firmware. It does not know about /usr/lib/firmware.
if [ -d /usr/lib/firmware ] && [ ! -e /lib/firmware ]; then
    ln -s ../usr/lib/firmware /lib/firmware
    log "linked /lib/firmware -> /usr/lib/firmware ($(find /usr/lib/firmware -type f 2>/dev/null | wc -l | tr -d ' ') files the kernel could not see)"
fi

# The development sync, so a fix does not need a rebuild to reach the machine.
#
# Deliberately installed and enabled on every image, not just development ones:
# the whole value is that a stick already in someone's hand can be fixed by
# copying files onto its ESP from any Mac. It does nothing at all unless
# /boot/nethos-dev exists, and costs one checksum at boot when it does not.
if [ -f "$PAYLOAD/systemd/nethos-devsync.service" ]; then
    install -m 0644 "$PAYLOAD"/systemd/nethos-devsync.service \
        /etc/systemd/system/nethos-devsync.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable nethos-devsync.service >/dev/null 2>&1 || true
fi

# The kernel fragment, so a machine can rebuild its own kernel without the
# repository checked out.
if [ -d "$PAYLOAD/kernel" ]; then
    install -d "$PREFIX/kernel"
    install -m 0644 "$PAYLOAD"/kernel/*.config "$PREFIX/kernel/"
fi

install -d /etc/xdg/xdg-desktop-portal
for d in sway Wayfire wlroots; do
    install -m 0644 "$PAYLOAD"/xdg/portals.conf \
        "/etc/xdg/xdg-desktop-portal/${d}-portals.conf"
done
install -m 0644 "$PAYLOAD"/xdg/portals.conf /etc/xdg/xdg-desktop-portal/portals.conf

# Where the assistant thinks. Never overwritten: this is the one file the user
# edits to move between the local model and a cloud endpoint, and an update
# that reset it would put a 10GB model back on a machine they had moved off it.
if [ ! -f /etc/nethos/assistant.conf ]; then
    install -m 0644 "$PAYLOAD"/assistant/assistant.conf /etc/nethos/assistant.conf
fi

install -d /etc/systemd/user
install -m 0644 "$PAYLOAD"/systemd/nethosd.service /etc/systemd/user/nethosd.service

# Compressed swap, as a system unit rather than a user one: it has to exist
# before a session does. The machine shipped with no swap at all, which makes
# an OOM kill the first symptom of running short rather than a slowdown.
if [ -f "$PAYLOAD"/systemd/nethos-memory.service ]; then
    install -m 0644 "$PAYLOAD"/systemd/nethos-memory.service \
        /etc/systemd/system/nethos-memory.service
    systemctl daemon-reload >/dev/null 2>&1 || true
    systemctl enable nethos-memory.service >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------
log "User configuration"
# --------------------------------------------------------------------------
# Create each level explicitly: `install -d -o user a/b` only applies the
# ownership to the leaf, leaving a root-owned ~/.config behind. That breaks
# every GTK/Chromium app later (Chromium fails to resolve its crashpad
# database path and aborts at startup), so it is worth being explicit.
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.config"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.config/sway"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.config/hypr"
ln -sf /etc/nethos/hyprland.conf "$NETH_HOME/.config/hypr/hyprland.conf"
chown -h "$NETH_USER:$NETH_USER" "$NETH_HOME/.config/hypr/hyprland.conf"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local/share"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local/share/nethos"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local/share/nethos/apps"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local/state"
install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.local/state/nethos"

# The XDG user directories. Debian creates these from xdg-user-dirs, which
# runs from a maintainer script we never execute, so on a fresh install the
# home directory has nothing in it but dotfiles.
#
# Both of the things people report as broken about files come from that. The
# desktop asks /api/files for ~/Desktop, gets a 404 because there is no such
# folder, and gives up -- so there are no icons, and "the desktop icons do not
# work" is really "there are no icons to click". And places() lists only the
# directories that exist, so the sidebar in Files is empty as well, which
# reads as a file manager that has not finished being written.
for d in Desktop Documents Downloads Pictures Music Videos; do
    install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/$d"
done

ln -sf /etc/sway/config "$NETH_HOME/.config/sway/config"
chown -h "$NETH_USER:$NETH_USER" "$NETH_HOME/.config/sway/config"

# The same face for GTK applications. Installing the font only puts it on
# disk; nothing selects it, so Thunar and every dialog would go on rendering
# in Cantarell next to a shell rendering in Nunito. Both toolkit versions,
# because the system has GTK3 and GTK4 applications side by side.
for gtkdir in gtk-3.0 gtk-4.0; do
    install -d -o "$NETH_USER" -g "$NETH_USER" "$NETH_HOME/.config/$gtkdir"
    cat > "$NETH_HOME/.config/$gtkdir/settings.ini" <<'GTKINI'
[Settings]
gtk-font-name=Nunito 11
GTKINI
    chown "$NETH_USER:$NETH_USER" "$NETH_HOME/.config/$gtkdir/settings.ini"
done

if [ "$FILES_ONLY" -eq 0 ]; then
    log "Autologin and session start"
    install -d /etc/systemd/system/getty@tty1.service.d
    cat >/etc/systemd/system/getty@tty1.service.d/autologin.conf <<EOF
[Service]
ExecStart=
ExecStart=-/usr/bin/agetty --autologin ${NETH_USER} --noclear %I \$TERM
EOF

    cat >"$NETH_HOME/.bash_profile" <<'EOF'
# NETHOS: start the desktop on the first virtual terminal, nowhere else.
if [ -z "${WAYLAND_DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
    export XDG_SESSION_TYPE=wayland
    export MOZ_ENABLE_WAYLAND=1
    export QT_QPA_PLATFORM=wayland
    export GDK_BACKEND=wayland
    export _JAVA_AWT_WM_NONREPARENTING=1

    # Without a GPU, wlroots refuses a software renderer unless told to.
    export WLR_RENDERER_ALLOW_SOFTWARE=1
    export WEBKIT_DISABLE_COMPOSITING_MODE=1
    # LIBGL_ALWAYS_SOFTWARE is not forced: Mesa refuses it once the compositor
    # has opened a real DRM node ("Not allowed to force software rendering when
    # API explicitly selects a hardware device"), EGL fails, and the compositor
    # exits immediately to a black screen. Let Mesa fall back on its own.

    # Wayfire is the NETHOS look: windows float and stack instead of tiling,
    # firedecor gives every window -- not just NETHOS's own -- a real rounded
    # frame with working minimise/maximise/close, and blur is the
    # compositor's job instead of a per-frame CSS cost (see wayfire.ini).
    # It also snaps on drag natively, which sway has no concept of and
    # nethosd used to fake by polling -- badly enough on real hardware to be
    # worth removing rather than continuing to patch.
    #
    # Hyprland was tried first here once, on the theory that it was the only
    # one of the two that could do this -- it was never actually reachable,
    # because Debian does not package it, so every session fell through to
    # sway regardless of that check passing or failing. Wayfire is packaged
    # (wayfire, reform-firedecor) and is what wayfire.ini is written for.
    # sway remains the fallback for wherever Wayfire itself will not start;
    # nethosd speaks both, so NETHOS_COMPOSITOR=sway in the environment gets
    # you the old session.
    case "${NETHOS_COMPOSITOR:-wayfire}" in
        wayfire)
            if command -v wayfire >/dev/null; then
                export XDG_CURRENT_DESKTOP=wayfire
                exec wayfire
            fi
            ;;
        hyprland)
            if command -v Hyprland >/dev/null; then
                export XDG_CURRENT_DESKTOP=Hyprland
                exec Hyprland
            fi
            ;;
    esac
    export XDG_CURRENT_DESKTOP=sway
    exec sway
fi
EOF
    chown "$NETH_USER:$NETH_USER" "$NETH_HOME/.bash_profile"
    systemctl enable systemd-logind >/dev/null 2>&1 || true
fi

# --------------------------------------------------------------------------
log "Update channel"
# --------------------------------------------------------------------------
install -d /etc/nethos
if [ ! -f /etc/nethos/update.conf ]; then
    cat >/etc/nethos/update.conf <<'EOF'
# Where `nethos-update` pulls system files from. Change REPO_URL to your own
# fork and NETHOS updates from your repository instead.
REPO_URL="https://github.com/Caleb22589/nethos.git"
BRANCH="main"
EOF
fi

# --------------------------------------------------------------------------
if [ "$FILES_ONLY" -eq 0 ]; then
    log "Branding"
    # /etc/os-release normally symlinks to the filesystem package's file;
    # replace it with a real file so pacman upgrades don't clobber branding.
    rm -f /etc/os-release
    cat >/etc/os-release <<'EOF'
NAME="NETHOS"
PRETTY_NAME="NETHOS"
ID=nethos
ID_LIKE=arch
BUILD_ID=rolling
ANSI_COLOR="0;36"
HOME_URL="https://localhost/"
LOGO=nethos
EOF

    cat >/etc/issue <<'EOF'

  \e[36mN E T H O S\e[0m  —  \e[90ma browser-shell Linux, built on Arch\e[0m
  \e[90m\r on \m — \l\e[0m

EOF

    hostnamectl set-hostname nethos 2>/dev/null || echo nethos >/etc/hostname
fi

date -u +'%Y-%m-%dT%H:%M:%SZ' >/etc/nethos-release

# Belt and braces: nothing under the user's home should be root-owned.
chown -R "$NETH_USER:$NETH_USER" "$NETH_HOME"

if [ "$FILES_ONLY" -eq 1 ]; then
    log "Files updated. Run 'nethos-reload --daemon' to apply without a reboot."
else
    log "NETHOS installed. Rebooting into the desktop."
fi
