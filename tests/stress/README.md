# tests/stress — retained execution harnesses (contention lane)

Test instruments are never disposed of. Every harness a review or diagnosis round builds
and validates gets committed here, verbatim-plus-parametrization, with its provenance and
the numbers it produced — regression evidence is only evidence while the instrument that
produced it can be re-run. (Ruling: Thomas, 2026-08-10, during the tsk race series.)

**Not part of the default suite, on purpose.** These loops deliberately create process
and scheduling contention, and several drive real `systemd-run --user` units. Run them at
release gates on an otherwise idle box (on shared hardware: behind the GPU/CPU tenancy
arbiter), never alongside latency-sensitive work. `tests/run.sh` discovers only
`tests/test-*.sh`; nothing here runs unless invoked explicitly.

Run one directly (each takes iteration-count args, see its header), or all with small
counts via:

    bash tests/stress/run-stress.sh          # release-gate defaults
    bash tests/stress/run-stress.sh quick    # smoke: a handful of iterations each

## Intake convention

A round that builds a harness in scratch hands it back to the driving session at verdict
time; the session commits it here with: (1) a header naming the round, the defect or
property it targets, and the measured result at the time; (2) paths parametrized
(`TSK`/repo-relative discovery, state under `mktemp -d`); (3) an entry in the table below.
A brief that grants a reviewer scratch-only writes must also ask for the instruments back
— scratch is session-scoped tmpfs, and an uncommitted instrument reads as *never built*
to the next session.

## Instruments

| file | targets | provenance | result when retained |
|---|---|---|---|
| `stress-lied-manager-race.sh` | the DEEP duplicate-start window: a manager that mis-reports a live unit as inactive (stateful stub), looped end-to-end over run→race→stop | diagnosis round, 2026-08-10 (pre-fix repro of the command-substitution race) | pre-fix: 41/1300 failures at ambient load, uniform substituted-command signature; post-fix (flock series): the stubbed deep window remains reachable by design — the harness MEASURES its substitution rate (first full run: 1/200) as a reported residual, and fails only on outcomes outside the two known ones; closing the window outright = the unique-runner-path rework, queued |
| `stress-concurrent-start.sh` | the REAL concurrent-start substitution race: N truly-parallel same-name `tsk run`s per iteration; asserts exactly one winner, every loser refuses with a known message, and no loser's marker reaches the winner's log | verification rounds, 2026-08-10 (post-fix) | 1,140 iterations, 0 failures at 9a78dfb; 300×4-way re-run clean at 91331eb |
