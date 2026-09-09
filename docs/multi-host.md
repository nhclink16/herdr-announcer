# Attached over SSH? Voices that follow you

Sound plays on the machine running the Herdr server — a plain SSH session
cannot carry audio to your local speakers. Easiest first:

1. `toast = true` — with `[ui.toast] delivery = "terminal"` in your Herdr
   config, the summary arrives as a native notification on your local
   machine, through SSH.
2. `speak_command` — route the text somewhere audible: an ntfy push, or
   text-to-speech on the machine you're at, if the server can SSH back:

   ```toml
   speak_command = ["ssh", "my-mac", "say"]
   ```

## Following you between devices

That `speak_command` is fixed to one machine. If you attach from several —
a desktop, a laptop, sometimes sitting at the host itself —
[`examples/route-speak.sh`](../examples/route-speak.sh) detects where you
actually are and speaks on every attached device, falling back to the host
when nobody is remote. It runs on macOS and Linux hosts alike — it picks the
local voice automatically (`say`, `espeak-ng`, `spd-say`, `espeak`), detects
which `nc` flavor it has for the liveness probe, and reads connections from
`ss` or `netstat`, whichever exists. List your machines and point
`speak_command` at it:

```toml
speak_command = ["/path/to/route-speak.sh"]
```

```sh
HOSTS="
desktop|10.0.0.5|windows
laptop|10.0.0.6|macos
"
```

Backends are `macos`, `windows`, `linux`, or `cmd:<anything reading stdin>`
(`cmd:ntfy publish mytopic` works fine).

## Headless servers

A build box or a Raspberry Pi running its own Herdr server has no speakers,
so "nobody is attached, speak locally" means the announcement is lost. Name
the desk that should hear it instead:

```sh
FALLBACK_HOST="imac|100.123.220.38|macos"
```

When presence detection finds nobody, the script speaks at that host if its
SSH port answers, and only then gives up with a logged error. Presence still
wins when it can be detected, so the announcement follows you as before.

## Why the script is shaped the way it is

**Detected is not reachable.** A sleeping or powered-off machine leaves its
`ESTABLISHED` TCP entry behind for a long time, so presence detection alone
will happily route speech to a host that is gone — and then the announcement
is lost while ssh waits out its timeout. The script probes the SSH port with
a short `nc -z` before believing a host is present, and if *no* host actually
accepted the speech it falls back to the local machine rather than dropping
it. Silent loss is the worst failure mode here: everything exits 0 and you
simply stop hearing announcements.

**`who` alone is not enough.** It only sees interactive SSH logins, which
have a TTY and a utmp entry. `herdr --remote <host>` attaches over `ssh -T` —
no TTY, no utmp entry — so `who` reports nothing and a detector built on it
silently misses every remote attach. The script also checks for an
ESTABLISHED connection arriving at its own SSH port, which is the signal that
catches it.

That direction check matters. Your Herdr host likely holds *outbound* SSH
sessions to the same machines — including the ones this script opens to
speak — and matching those would make every peer look permanently present.

**Quoting, if you write your own.** The announcer sanitizes every summary to
letters, digits, and basic punctuation before it reaches `speak_command` —
summaries come from LLMs reading untrusted agent output, and a summary that
can smuggle `$(...)` into a remote shell command is a security hole, not a
quoting bug. Even so, prefer handing the text over stdin (macOS `say` and
`espeak-ng` both read it) instead of splicing `{text}` into a command string;
stdin has no quoting rules to get wrong.
