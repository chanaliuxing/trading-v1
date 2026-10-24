# AutoGen Agent Workflow for System Engineering

## Objective
Orchestrate requirement-to-acceptance using explicit specialist agents with adversarial review and hard gates.

## Agent Roles
1. RequirementsAgent
- Input: user goals, current architecture, constraints.
- Output: PRD draft, IO schema draft, acceptance draft.

2. ReviewAgent
- Input: RequirementsAgent output.
- Output: document-level issues, ambiguity list, missing constraints.

3. CriticalAgent
- Input: reviewed draft.
- Output: adversarial critique, failure modes, rollback and compliance challenges.

4. ReviewAgent (Revision Pass)
- Input: CriticalAgent findings.
- Output: revised final spec package with explicit decisions.

5. ValidateAgent
- Input: final spec package.
- Output: schema consistency report, requirement traceability matrix (RTM), gate verdict.

6. DevAgent
- Input: validated specs.
- Output: implementation PRs, migration notes, test evidence.

7. AcceptanceAgent
- Input: DevAgent evidence.
- Output: acceptance verdict, smoke report, release recommendation.

## Handoff Artifacts
- `prd.md`
- `io_schema.md` + JSON schemas
- `acceptance.md`
- `risk_register.md`
- `rtm.csv` (requirement -> tests -> evidence)
- `release_checklist.md`

## AutoGen Conversation Topology
- Topology: sequential pipeline with explicit gate checks.
- No direct DevAgent start until ValidateAgent status is PASS.
- CriticalAgent can block pipeline by severity >= HIGH unresolved items.

## Gate Rules
- Gate A (Spec Completeness):
  - PRD has goals/non-goals/edge cases/rollout.
  - IO schemas compile and examples validate.
- Gate B (Adversarial Closure):
  - All HIGH findings from CriticalAgent resolved or risk accepted with owner.
- Gate C (Pre-Implementation Validation):
  - RTM coverage >= 95%.
- Gate D (Implementation Readiness):
  - Branch plan, rollback plan, smoke plan present.
- Gate E (Release):
  - AcceptanceAgent PASS and smoke test PASS.

## Suggested AutoGen Team Config (Conceptual)
- Team: `requirements`, `review`, `critical`, `validate`, `dev`, `acceptance`
- Shared memory: immutable artifact index + decision log.
- Tooling:
  - repository read/write
  - schema lint/validator
  - test runner
  - diff summarizer

## Minimal Prompt Contracts
- RequirementsAgent must produce machine-checkable acceptance criteria.
- ReviewAgent must cite exact section and required change.
- CriticalAgent must include exploit/failure scenario and impact.
- ValidateAgent must produce PASS/FAIL with blocking list.
- DevAgent must map each commit/test to RTM entries.
- AcceptanceAgent must verify evidence, not narrative.

## Output Contract Example
- Status: `PASS | FAIL | BLOCKED`
- Blocking items:
  - ID, severity, owner, due phase
- Evidence links:
  - file path, test command, artifact path
