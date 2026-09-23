# Gate Rules

A gate is justified only when it protects a concrete failure boundary.

Before adding or keeping a deterministic gate, answer:

1. What concrete failure/risk does it prevent?
2. What is the smallest identity/boundary where that risk exists?
3. What evidence can be checked there?
4. What happens when the gate fails?
5. Why is agent judgment insufficient?
6. Which existing gate already overlaps this risk?
7. When could this gate be deleted or merged?

Reject or merge a gate when:
- no concrete failure is named;
- its scope is broader than the protected failure;
- another gate already protects the same boundary;
- it only synchronizes duplicate internal representations;
- it converts provider uncertainty into permanent domain semantics unnecessarily;
- it blocks unrelated work.

Tests are evidence of behavior, not proof that every architectural gate is necessary.

Prefer the smallest invariant that safely prevents the failure.
