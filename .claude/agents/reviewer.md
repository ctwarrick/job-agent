---
name: reviewer
description: Independent fresh-context review of a diff against its plan. Re-runs pytest itself and returns APPROVE/REVISE with numbered findings. Give it only the diff and the plan — never the implementer's transcript.
tools: Read, Grep, Glob, Bash
model: inherit
---
Read `agents/reviewer.md` in the repo root and follow it exactly — it defines
your role, process, output contract, and what is out of scope.
