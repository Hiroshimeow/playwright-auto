---
name: legacy-modernization
description: Use when improving or migrating a working legacy system while preserving useful behavior and operational safety without copying its existing architecture.
---

# Legacy Modernization

Treat the legacy system as a behavioral oracle, incident database, and compatibility evidence source—not as the target architecture.

For each slice:
1. Identify one operator/user-visible capability.
2. Find the incidents/tests/rules that explain why it exists.
3. Extract the minimum true invariant.
4. Map that invariant to one clean owner in the current architecture.
5. Write black-box/invariant acceptance evidence.
6. Implement the smallest mechanism.
7. Delete/retire duplicate legacy machinery when safe.

Explicitly name what is **not** being copied.

Red flags:
- translating old classes one-for-one;
- keeping two authorities during an indefinite migration;
- preserving internal report/schema/state parity without a real consumer;
- creating generic registries/frameworks before a concrete second use case;
- adding one recovery branch per historical incident.

Goal: keep the memory of the old system, not its shape.
