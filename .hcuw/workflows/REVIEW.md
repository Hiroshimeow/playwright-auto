# Review Workflow

Review actual repository state and evidence independently from the implementing agent's summary.

Check in this order:

1. Does behavior satisfy the stated acceptance?
2. Are important invariants preserved under failure/restart/concurrency?
3. Did the change introduce a second authority, duplicate path, or unnecessary gate?
4. Is ownership clearer or more tangled than before?
5. Are provider/tool quirks contained at their boundary?
6. Are tests checking stable behavior rather than implementation trivia?
7. Is any old path now obsolete and safe to remove?
8. Are docs/spec/current-state claims consistent with code?

A review finding should be concrete and reproducible. Avoid generic style commentary unless it affects maintainability or correctness.
