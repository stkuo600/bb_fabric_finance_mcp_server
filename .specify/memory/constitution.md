<!--
Sync Impact Report
===================
- Version change: N/A → 1.0.0 (initial ratification)
- Added principles:
  - I. Code Quality
  - II. Testing Standards
  - III. User Experience Consistency
  - IV. Performance Requirements
- Added sections:
  - Development Standards
  - Quality Gates
  - Governance
- Templates requiring updates:
  - .specify/templates/plan-template.md — ✅ compatible (Constitution Check section exists)
  - .specify/templates/spec-template.md — ✅ compatible (Success Criteria covers performance/UX)
  - .specify/templates/tasks-template.md — ✅ compatible (test-first workflow aligns with Principle II)
- Follow-up TODOs: none
-->

# BB Fabric Finance MCP Server Constitution

## Core Principles

### I. Code Quality

All production code MUST meet the following non-negotiable standards:

- Every module MUST have a single, clearly defined responsibility.
- Functions MUST NOT exceed 50 lines; files MUST NOT exceed 400 lines
  without explicit justification in a Complexity Tracking table.
- All public interfaces MUST include type annotations.
- Dead code, commented-out blocks, and TODO markers MUST NOT be
  merged into the main branch.
- Linting and formatting checks MUST pass before any commit is
  accepted (enforced via pre-commit hooks).
- Code review MUST verify adherence to these rules before merge.

**Rationale**: A finance-domain MCP server demands correctness and
auditability. Strict code quality reduces defect surface and eases
long-term maintenance.

### II. Testing Standards

Testing is mandatory and follows a layered strategy:

- **Unit tests** MUST cover every public function and method.
  Minimum line coverage threshold: 80%.
- **Contract tests** MUST verify every MCP tool/resource endpoint
  against its published schema.
- **Integration tests** MUST validate end-to-end flows involving
  external services (database, APIs) using real connections or
  officially supported test doubles — never ad-hoc mocks.
- Tests MUST be written before or alongside implementation
  (test-first or test-alongside; never test-after-ship).
- All tests MUST pass in CI before a PR can be merged.
- Flaky tests MUST be quarantined and fixed within one sprint;
  they MUST NOT block unrelated PRs.

**Rationale**: Financial data operations are high-stakes. Thorough,
layered testing catches regressions at the boundary where they are
cheapest to fix.

### III. User Experience Consistency

Every MCP tool and resource exposed by this server MUST provide a
consistent and predictable experience:

- Tool naming MUST follow the pattern `<domain>_<verb>_<noun>`
  (e.g., `finance_get_transactions`).
- Error responses MUST use a uniform structure containing `code`,
  `message`, and optional `details` fields.
- All user-facing text (tool descriptions, error messages, parameter
  descriptions) MUST be written in clear, professional English.
- Parameter naming MUST use `snake_case` consistently across all
  tools.
- Breaking changes to tool signatures MUST follow the deprecation
  procedure defined in Governance before removal.

**Rationale**: MCP clients (LLMs and humans) rely on predictable
conventions to discover and use tools effectively. Inconsistency
increases integration errors and user frustration.

### IV. Performance Requirements

The server MUST meet the following performance baselines under
normal operating conditions:

- Tool invocations MUST respond within 2 seconds (p95) for
  operations that do not involve external I/O beyond the server's
  own data store.
- Operations involving external APIs MUST respond within 10 seconds
  (p95) and MUST implement timeout + retry with exponential backoff.
- Memory consumption MUST NOT exceed 512 MB under typical workload.
- Startup time MUST NOT exceed 5 seconds.
- All performance-critical paths MUST be profiled before release
  when changes are made to them.
- Performance regressions exceeding 20% on any tracked metric MUST
  block the release until resolved or explicitly waived.

**Rationale**: An MCP server is invoked in interactive contexts
where latency directly impacts perceived quality. Strict performance
gates prevent silent degradation.

## Development Standards

- **Language**: Python 3.11+ (or as specified in plan.md).
- **Dependency management**: All dependencies MUST be pinned to
  exact versions in lock files.
- **Secrets**: Credentials, API keys, and tokens MUST NOT appear in
  source code or committed configuration. Use environment variables
  or a secrets manager.
- **Logging**: Structured JSON logging MUST be used for all
  operational events. Log levels MUST follow severity semantics
  (DEBUG, INFO, WARNING, ERROR, CRITICAL).
- **Documentation**: Public APIs MUST have docstrings. Architecture
  decisions MUST be recorded in the spec or plan artifacts.

## Quality Gates

Every pull request MUST pass the following gates before merge:

1. **Lint gate**: Zero linting errors (configured tool rules).
2. **Type-check gate**: Zero type errors (mypy strict or equivalent).
3. **Test gate**: All unit, contract, and integration tests pass.
4. **Coverage gate**: Line coverage >= 80% on changed files.
5. **Performance gate**: No tracked metric regresses > 20%.
6. **Review gate**: At least one approving review from a team member
   who did not author the PR.
7. **Constitution gate**: Reviewer MUST verify that the PR does not
   violate any principle in this document.

## Governance

- This constitution supersedes conflicting guidance in all other
  project documents. If a template or workflow contradicts a
  principle here, this document wins.
- **Amendments** require:
  1. A written proposal describing the change and its rationale.
  2. Review and approval by the project lead.
  3. A migration plan for any existing code or workflows affected.
  4. An updated version number following semantic versioning:
     - MAJOR: principle removal or backward-incompatible redefinition.
     - MINOR: new principle or materially expanded guidance.
     - PATCH: clarifications, wording, or typo fixes.
- **Deprecation of MCP tools**: Deprecated tools MUST remain
  functional for at least one release cycle with a deprecation
  notice before removal.
- **Compliance review**: At the start of each feature, the plan.md
  Constitution Check section MUST enumerate how the feature satisfies
  each principle. Violations MUST be justified in the Complexity
  Tracking table.

**Version**: 1.0.0 | **Ratified**: 2026-04-02 | **Last Amended**: 2026-04-02
