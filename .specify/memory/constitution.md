<!--
Sync Impact Report
Version change: 1.0.0 -> 2.0.0
Reason: remove duplicated project engineering policy from Spec Kit constitution and delegate to the canonical HCU rulebook.
Modified principles: runtime-v2 engineering principles -> Spec Kit governance bridge
Added sections: Canonical Policy Delegation; Feature Scope; Tooling Scope
Removed sections: duplicated project-wide runtime/code rules
Follow-up TODOs: regenerate HCU evidence fingerprint when `hcuw` CLI is available on fjp
-->
# HCUW Spec Kit Constitution

## I. Canonical Policy Delegation

`.agents/rules/project.md` is the single repository-specific normative engineering rulebook. Spec Kit workflows MUST load and obey that rulebook. This constitution MUST NOT maintain a second copy of project-wide architecture, code organization, recovery, git, or safety rules.

When a Spec Kit artifact needs a project rule, reference its stable HCU rule ID (for example `HCU-ARCH-001`) rather than restating the rule.

## II. Feature Scope

Artifacts under `specs/<feature>/` own feature-specific requirements, public contracts, acceptance criteria, non-goals, design choices, and task ordering for that feature only.

A feature spec MAY strengthen a requirement for its own scope, but MUST NOT silently redefine repository-wide policy. If a feature discovers a reusable project-wide rule, promote it through an explicit reviewed edit to `.agents/rules/project.md` and then reference the new rule ID from the feature.

## III. Tooling Scope

`.specify/` contains Spec Kit tooling, templates, integration metadata, and this compatibility constitution. `.agents/skills/speckit-*` contains Spec Kit workflow procedures. Neither location is a project engineering rule owner.

Spec Kit-generated files MUST preserve external tool compatibility while delegating project-specific policy to `.agents/rules/project.md`.

## IV. Conflict Handling

Precedence for Spec Kit work is:

1. current explicit user/task instruction;
2. `.agents/rules/project.md` project-wide policy;
3. active feature-specific `specs/<feature>/` contract;
4. repository source/tests/tool configuration within their owning boundary;
5. generic skill/tool defaults.

A material conflict MUST be surfaced explicitly. Do not resolve a conflict by copying one rule into another authority location.

## Governance

This constitution is intentionally a bridge, not a second rulebook. Amend it only when Spec Kit integration/governance changes. Project engineering policy changes belong in `.agents/rules/project.md`.

**Version**: 2.0.0 | **Ratified**: 2026-09-30 | **Last Amended**: 2026-09-30
