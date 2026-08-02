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
