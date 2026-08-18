#!/bin/sh
# Presence-aware, multi-host speech router for herdr-announcer.
#
# Point speak_command at this script:
#
#   speak_command = ["/path/to/route-speak.sh"]
#
# It reads the announcement on stdin and speaks it on every machine you are
# currently attached from, falling back to the Herdr host itself when you are
# sitting at it. Announcements follow you between devices with no config
# changes.
#
# ---------------------------------------------------------------------------
# Why two detection signals
#
#   who           sees interactive SSH logins — a real TTY, a utmp entry.
#   inbound_from  sees everything else. This is the one that matters for
#                 `herdr --remote <host>`, which attaches over `ssh -T`. No
#                 TTY means no utmp entry, so `who` reports nothing at all.
#                 Detection built only on `who` silently misses every
#                 herdr --remote attach.
#
# inbound_from deliberately matches only connections *arriving* at this
# machine's SSH port. Your Herdr host probably holds outbound SSH sessions to
# the very machines listed below — including the ones this script opens to
# speak. Matching those would make every peer look permanently present.
# ---------------------------------------------------------------------------

# One line per machine:  ssh-alias | ip | backend
#
# backend is either a builtin (macos, windows, linux) or any command that
# reads text on stdin, written as  cmd:<command>  e.g.  cmd:espeak-ng
#
# The alias must be resolvable by ssh (use ~/.ssh/config), and the ip must be
# the address that machine connects to you *from*.
HOSTS="
desktop|10.0.0.5|windows
laptop|10.0.0.6|macos
"

LOCAL_SPEAK=""
LOCAL_SPEAK_MODE=""
if command -v say >/dev/null 2>&1; then
  LOCAL_SPEAK=$(command -v say)
  LOCAL_SPEAK_MODE=say
elif command -v espeak-ng >/dev/null 2>&1; then
  LOCAL_SPEAK=$(command -v espeak-ng)
  LOCAL_SPEAK_MODE=espeak-ng
elif command -v spd-say >/dev/null 2>&1; then
  LOCAL_SPEAK=$(command -v spd-say)
  LOCAL_SPEAK_MODE=spd-say
elif command -v espeak >/dev/null 2>&1; then
  LOCAL_SPEAK=$(command -v espeak)
  LOCAL_SPEAK_MODE=espeak
else
  printf '%s\n' 'route-speak: no local say, espeak-ng, spd-say, or espeak found' >&2
fi

SSH_PORT=22

if nc -h 2>&1 | grep -q -- '-G'; then
  NC_FLAVOR=macos
else
  NC_FLAVOR=portable
fi

text=$(cat)
[ -n "$text" ] || exit 0

local_speak() {
  [ -n "$LOCAL_SPEAK" ] || return 1
  if [ "$LOCAL_SPEAK_MODE" = spd-say ]; then
    printf '%s\n' "$text" | "$LOCAL_SPEAK" -e -w
  else
    printf '%s\n' "$text" | "$LOCAL_SPEAK"
  fi
}

ssh_with_opts() {
  ssh -o ConnectTimeout=5 -o BatchMode=yes "$@"
}

# True when $1 has an ESTABLISHED connection into our SSH port.
inbound_from() {
  if command -v ss >/dev/null 2>&1; then
    ss -tan 2>/dev/null | awk -v ip="$1" -v port="$SSH_PORT" '
      $1 == "ESTAB" {
        local_endpoint = $4
        peer_endpoint = $5
        sub(/^.*:/, "", local_endpoint)
        sub(/:[^:]*$/, "", peer_endpoint)
        gsub(/^\[/, "", peer_endpoint)
        gsub(/\]$/, "", peer_endpoint)
        if (local_endpoint == port && peer_endpoint == ip) hit = 1
      }
      END { exit !hit }
    '
    return
  fi

  netstat -an 2>/dev/null | awk -v ip="$1" -v port="$SSH_PORT" '
    $1 ~ /^tcp/ && $6 == "ESTABLISHED" {
      local_endpoint = $4
      peer_endpoint = $5
      if (local_endpoint ~ /\.[0-9]+$/) {
        sub(/^.*\./, "", local_endpoint)
      } else {
        sub(/^.*:/, "", local_endpoint)
      }
      if (peer_endpoint ~ /\.[0-9]+$/) {
        sub(/\.[0-9]+$/, "", peer_endpoint)
      } else {
        sub(/:[^:]*$/, "", peer_endpoint)
      }
      if (local_endpoint == port && peer_endpoint == ip) hit = 1
    }
    END { exit !hit }
  '
}

# A sleeping peer leaves its ESTABLISHED entry behind, so "detected" is not
# "reachable". Probe the SSH port with a short timeout before believing it.
reachable() {
  if [ "$NC_FLAVOR" = macos ]; then
    nc -z -G 2 "$1" "$SSH_PORT" >/dev/null 2>&1
  else
    nc -z -w 2 "$1" "$SSH_PORT" >/dev/null 2>&1
  fi
}

present() {
  ip_escaped=$(printf '%s' "$1" | sed 's/\./\\./g')
  { who 2>/dev/null | grep -qE "[( ]${ip_escaped}[) ]" || inbound_from "$1"; } &&
    reachable "$1"
}

speak_on() {
  _alias=$1
  _backend=$2
  case "$_backend" in
    macos)
      # say reads stdin, which sidesteps shell quoting entirely.
      printf '%s\n' "$text" | ssh_with_opts "$_alias" '/usr/bin/say'
      ;;
    linux)
      printf '%s\n' "$text" | ssh_with_opts "$_alias" \
        'if command -v espeak-ng >/dev/null 2>&1; then espeak-ng; elif command -v spd-say >/dev/null 2>&1; then spd-say -e -w; else espeak; fi'
      ;;
    windows)
      # PowerShell escapes a single quote by doubling it. Skip this and any
      # apostrophe — "it's", "the agent's" — breaks the command. Summaries are
      # generated prose, so apostrophes are a matter of time, not chance.
      _escaped=$(printf '%s' "$text" | sed "s/'/''/g")
      ssh_with_opts "$_alias" "powershell -NoProfile -Command \"Add-Type -AssemblyName System.Speech; (New-Object System.Speech.Synthesis.SpeechSynthesizer).Speak('$_escaped')\""
      ;;
    cmd:*)
      printf '%s\n' "$text" | ssh_with_opts "$_alias" "${_backend#cmd:}"
      ;;
    *)
      printf 'route-speak: unknown backend "%s" for %s\n' "$_backend" "$_alias" >&2
      return 1
      ;;
  esac
}

# Collect the attached machines first. Backgrounding inside a pipeline would
# put the jobs in a subshell where `wait` cannot reach them, so gather into a
# variable and iterate with `for` in this shell.
attached=$(
  printf '%s\n' "$HOSTS" | while IFS='|' read -r alias ip backend; do
    [ -n "$alias" ] || continue
    [ "${alias#\#}" = "$alias" ] || continue   # skip commented lines
    present "$ip" && printf '%s|%s\n' "$alias" "$backend"
  done
)

if [ -z "$attached" ]; then
  local_speak
  exit $?
fi

# Speak everywhere at once rather than one device after another. Collect exit
# statuses: a host can pass the liveness probe and still fail to deliver (it
# sleeps mid-run, sshd refuses, the backend is missing). If nothing lands, the
# announcement must not be silently dropped.
saved_ifs=$IFS
IFS='
'
_pids=""
for entry in $attached; do
  IFS=$saved_ifs
  speak_on "${entry%%|*}" "${entry#*|}" &
  _pids="$_pids $!"
  IFS='
'
done
IFS=$saved_ifs

_spoke=0
for _p in $_pids; do
  wait "$_p" && _spoke=1
done

# Nobody accepted it — say it here rather than lose it.
[ "$_spoke" = 1 ] || local_speak
