# Run one task of an implementation plan

You are given a plan file and one task number. Do that task and nothing else.

Read the plan's header, its "Global Constraints", and your task in full before touching anything. The plan names a spec; read the sections your task refers to. Every task's requirements include the Global Constraints.

Rules:

- One task per invocation, and one agent in the repository at a time. The tasks share a git tree and a Postgres staging schema.
- Work on the branch the plan names, never on `main`.
- Code blocks in the plan are the implementation. Transcribe them exactly. Do not rename, restructure, add abstractions, add dependencies, or "improve" them.
- Follow the steps in order and run every command the task states. A step that says "expected: FAIL" must be seen failing before you write the implementation.
- A failing check is information, not an obstacle. You may fix a transcription mistake or a genuine bug in the implementation code. You may never: change a test's expected values, loosen a tolerance, skip or delete a test, edit a pinned hash, or touch a file the plan marks as an oracle. If the only way to get green is one of those, stop and report.
- If the plan itself looks wrong (the code cannot work as written, or contradicts the spec), stop and report with the exact error and your reading of the cause. Do not improvise a different design.
- Commit with the message the task gives, only the files the task lists.

If you were told you are a transcription worker: on the first unexpected failure, stop and report. Do not debug.

Report: files created or changed, each command run with its actual output (not a summary of it), the commit hash, and every deviation from the plan, however small. "No deviations" must be stated explicitly.
