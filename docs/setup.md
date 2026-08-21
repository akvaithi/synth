# Deploying Synth on a fresh macOS VM

## Prerequisites the machine must already have

- A GUI console session, signed into iCloud, with Calendar, Reminders, Notes and Mail
  configured. Synth drives the real apps; it does not talk to iCloud directly.
- `claude` logged in via `/login`. Remote Control and headless runs both require a
  subscription login; API keys are not supported for Remote Control and must not be used.

## 1. Tooling

    curl -LsSf https://astral.sh/uv/install.sh | sh
    curl -fsSL https://claude.ai/install.sh | bash
    # cloudflared, only if you want the Claude app connector:
    curl -sSL -o cf.tgz https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-darwin-amd64.tgz

`screen` and `textutil` already ship with macOS; no Homebrew is needed anywhere.

## 2. Environment hygiene

These must all be **unset**, in the shell and in `~/.claude/settings.json`:

`ANTHROPIC_API_KEY` (would silently bill the API instead of the subscription),
`ANTHROPIC_BASE_URL`, `DISABLE_TELEMETRY`, `DO_NOT_TRACK`,
`CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`, `DISABLE_GROWTHBOOK` — the last four disable the
feature-flag evaluation Remote Control depends on.

Set `CLAUDE_CLIENT_PRESENCE_FILE` so pushes are suppressed while you are at the machine, and
enable **Push when Claude decides** and **Push when actions required** via `/config`.

## 3. The signing identity

Ad-hoc signatures are keyed by cdhash, which changes on every build, so TCC treats each
rebuild as a new program and re-prompts — fatal for an unattended daemon. Create a stable
self-signed identity once:

    openssl req -x509 -newkey rsa:2048 -keyout k.key -out k.crt -days 7300 -nodes \
      -subj "/CN=Synth Code Signing/O=Synth" \
      -addext "basicConstraints=critical,CA:false" \
      -addext "keyUsage=critical,digitalSignature" \
      -addext "extendedKeyUsage=critical,codeSigning"
    openssl pkcs12 -export -out k.p12 -inkey k.key -in k.crt -passout pass:synth \
      -name "Synth Code Signing" -keypbe PBE-SHA1-3DES -certpbe PBE-SHA1-3DES -macalg sha1
    security import k.p12 -k ~/Library/Keychains/login.keychain-db -P synth \
      -T /usr/bin/codesign -A

macOS `security` cannot read modern PKCS12 MACs, hence the legacy algorithm flags. The
certificate does not need to be *trusted* — `codesign` will use an untrusted self-signed
identity, and TCC only needs the identity to be stable.

## 4. Build and install

    ./applekit/build.sh
    uv sync
    cp launchd/*.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/page.akvaithi.synth.daemon.plist

Then grant permissions. The first daemon start raises Calendar and Reminders prompts; the
first Notes or Mail call raises Automation prompts. Approve them once — they persist across
rebuilds now that the identity is stable.

Full Disk Access is **optional**. It only makes mail change-detection event-driven instead of
a ~9s poll. Note that granting it to `Synth.app` does not work: launchd execs the inner
binary, so the entry has to be `Synth.app/Contents/MacOS/synthkit` itself.

## 5. Seed the database

    synth index      # after the first scan
    synth ocr        # scanned PDFs, slow, safe to run overnight
    synth enrich     # LLM extraction over the curated set

## 6. Turn it on

    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/page.akvaithi.synth.watch.plist
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/page.akvaithi.synth.brief-morning.plist
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/page.akvaithi.synth.brief-evening.plist
    launchctl bootstrap gui/$UID ~/Library/LaunchAgents/page.akvaithi.synth.session.plist

Attach to the live session with `screen -r synth`; detach with `ctrl-a d`.
