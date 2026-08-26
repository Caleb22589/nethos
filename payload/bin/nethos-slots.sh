# Slot lookup, shared.
#
# Sourced, not run -- `. nethos-slots.sh`. nethos-ab and nethos-chroot both
# need to turn "A" or "B" into a device and know which one is running, and
# a second copy of this is exactly the kind of drift that has caused
# unrelated-looking bugs elsewhere in this project (see CLAUDE.md on npkg
# postinst scripts): fix one copy, forget the other, and the two tools
# quietly disagree about which slot is which.
#
# Installed to /usr/bin alongside everything else in bin/ -- see the
# "Everything in bin/, rather than a list" loop in install-nethos.sh.

CONF=/etc/nethos/slots.conf
SLOT_FILE=/etc/nethos/slot

slot_dev() {    # A|B -> device
    local spec
    spec=$(sed -n "s/^slot_$(echo "$1" | tr 'A-Z' 'a-z')=//p" "$CONF")
    case "$spec" in
        LABEL=*) echo "/dev/disk/by-label/${spec#LABEL=}" ;;
        UUID=*)  echo "/dev/disk/by-uuid/${spec#UUID=}" ;;
        *)       echo "$spec" ;;
    esac
}

active_slot() { cat "$SLOT_FILE" 2>/dev/null || echo A; }
other_slot()  { [ "$(active_slot)" = A ] && echo B || echo A; }
