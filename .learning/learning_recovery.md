# Recovery trigger learning

- Preserve the exact accepted-send receipt, task/hop ownership, and conversation URL while releasing a blocked task; recovery must resume durable work rather than replay it.
- Treat immediate release, durable source repair, concrete defect recording, and reusable learning as separate decisions; `PROBLEM.md` holds incident evidence while trigger learning holds only generalized invariants.
- Claim only continuously blocked work after its eligibility window, process the oldest eligible target one at a time, and apply per-target plus inter-target cooldowns to prevent recovery thrash.
