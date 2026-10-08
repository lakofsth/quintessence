# Security policy

## Scope

quintessence is a local-first tool: a command-line program (`qq`) that reads and writes
Markdown files in a git repository on your own machine, plus an optional remote interface (MCP)
for reaching that store from another host. Both are in scope for security reports, as are the
setup script, the hooks, and the plugin manifest that ship with them.

Out of scope: the models, editors, and agent harnesses you point at the store – report those
to their own maintainers.

## Reporting a vulnerability

Please report privately, not in a public issue: **security@lakofsth.org**

Useful things to include, as far as you have them: the version or commit you tested, the
platform, what an attacker would gain, and the smallest reproduction you can manage.

## What to expect

- An acknowledgement that the report arrived and is being looked at.
- A fix shipped in a tagged release, with the reporter credited unless you prefer otherwise.

There is no bounty program.

## Supported versions

The latest tagged release. Fixes are not backported to earlier tags.

## Boundaries worth knowing (not vulnerabilities)

- **Store text goes to the model endpoints you configure.** Embedding, `qq ask` and the
  remote face send HEAD and memory text to the endpoints named in your config, through an
  opener that ignores `http_proxy`. Nothing scrubs secrets out of that text first: keep
  credentials out of HEADs and facts, and use `QQ_REDACT_FILE` / the remote deny lists to
  withhold whole topics. `qq-redact.sh` withholds topics from a reader; it is not a secret
  scanner.
- **The authoring gate is structural, not adversarial.** It reads its policy keys from the
  config file (never the environment) and `qq config set` changes them only from a terminal,
  but a session holding a shell as your user can still edit that file, point `QQ_CONFIG`
  elsewhere, or edit the proposal queue. It stops a well-meaning untrusted model from authoring
  a security-tagged topic alone; it does not stop a hostile one.
- **A project store is one you scaffolded.** Walk-up discovery accepts a `.quintessence/` only
  if it carries the scaffold `qq init --project` writes (and, when `QQ_PROJECT_STORES` is set,
  only if its root is listed). A `.quintessence/` that arrived inside a cloned or unpacked
  repository is ignored.
- **The MCP passthrough is narrower than the CLI.** `qq(args)` on the stdio server forwards an
  allowlist of verbs, refuses `probe:` references (they run a shell command) and the admin
  verbs, and nulls stdin; use the CLI in a terminal for those.
