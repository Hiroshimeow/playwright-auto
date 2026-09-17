# CDPA Evolution Lab v0 — bounded efficacy result

Date: 2026-09-17

## Result

The lab now provides a reproducible, offline, immutable-data lifecycle for one pre-registered incident:

`observe → propose class → static gate → explicit promote → verified load → rollback`.

Incident #0001 reconstructed successfully. Across three benchmark iterations:

- accepted direction: PASS in 3/3 iterations;
- negative controls: 3/3 rejected per iteration (timeout-only, user-owned-tab reuse, and overbroad all-timeout classification);
- normalized evaluation content hash: `dc7e8cea5e7e2eafcdda201cf1d5f05babecc08f73b450889da7f15311995dde` in all three iterations;
- invariant violations: 0;
- mutable surface categories covered: `policy_description`;
- elapsed time: 0.002514 seconds for this run, explicitly nondeterministic.

The normalized comparison excludes `started_at`, `finished_at`, and the evaluation document `sha256` derived from those timestamps. Verdicts, predicates, holdouts, invariants, reasons, metrics, and candidate identities are byte-stable.

## Pre/post comparison

Before this addition, CDPA had no structured immutable incident/candidate/evaluation/promotion lineage tool. This v0 demonstrates a bounded, reproducible pipeline and discrimination by an evaluator-owned gate. It does not execute candidate code and has no production runtime consumer.

## Claim limits

This result does not establish a general proposal success rate, independent rediscovery of the accepted fix, production incident reduction, developer-time savings, token/compute/cost savings, statistical significance, or generalization. No second independently pre-registered incident with complete local pre-state, accepted direction, and evaluator-owned oracle evidence was established during DEV; broader efficacy remains unmeasured and is owned by TEST.
