# PuppyOne Docs

PuppyOne is a Git-native cloud filesystem for AI agents and teams.

Read in this order:

0. [Getting Started (developers & contributors)](getting-started.md)
1. [Architecture Vision](architecture/00-vision.md)
2. [Version Engine](architecture/01-version-engine.md)
3. [Context Resources](architecture/02-context-resources.md)
4. [CLI](architecture/03-cli.md)
5. [Connectors](architecture/04-connectors.md)
6. [Git Remote Locator, Credential, And Access Point Contract](architecture/05-git-remote-accesspoint.md)
7. [Gateway And Access Boundary](architecture/06-gateway-access-point-split.md)
8. [Shadow Snapshots](architecture/08-shadow-snapshots.md)
9. [Context Entry Points](architecture/10-context-entrypoints.md)
10. [Context Entry Point Data Model](architecture/11-context-entrypoint-data-model.md)
11. [Project Authorization and Git Credentials](architecture/12-project-authorization-and-git-credentials.md)
12. [Database Release Governance](architecture/13-database-release-governance.md) — migration merge admission, phased releases, validation and completion evidence
13. [Template Registry](architecture/14-template-registry.md)
14. [Project-Owned Repository Targets](architecture/15-project-owned-repository-targets.md)
15. [Payment, Billing, Entitlements, and Usage](architecture/15-payment-billing-entitlements.md)
16. [Project Publish Control Plane](architecture/16-project-publish-control-plane.md)

- [Desktop Agent inference and personal credits](../backend/src/platform/managed_ai/README.md) maps the implemented module and links the canonical architecture/data/sandbox runbook in `puppy-issues`.

Product and frontend design:

- [Design Guidelines](design.md)
- [Product Visual System](frontend/product-visual-system.md)
- [Color Mode Architecture](frontend/color-mode-architecture.md)
- [ISSUE-029 Authorization Cutover](ops/issue-029-authorization-cutover.md)
- [Canonical Git Remote Rollout Runbook](ops/canonical-git-remote-rollout.md)
- [July 2026 Database Migration Transition](ops/database-migration-transition-2026-07.md)
- [ISSUE-039 Repository Target Cutover](ops/issue-039-repository-target-cutover.md)
- [Workspace Binding Removal](ops/workspace-binding-removal.md)
- [Local Web Preview and Resource Regression](ops/local-web-resource-lifecycle.md)

Document-level constructs:

- [Editor Save Construct](document/editor-save-construct.md)
- [Markdown Editor Architecture](document/markdown-editor-architecture.md)
- [VM / SSH Agent Access Architecture](document/vm-ssh-agent-access.md)

For product onboarding (install CLI, first project), see the [root README](../README.md).

The current source of truth for versioning is `backend/src/version_engine/`.
Historical planning, migration, rename, audit, and verification notes live in
`architecture/archive/` so they cannot be mistaken for active implementation
guidance.
