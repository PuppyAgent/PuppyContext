## ADDED Requirements

### Requirement: Native cloud Git workspace
The system SHALL clone the authorized full native knowledge repository inside
the sandbox and preserve history and the selected starting branch.

#### Scenario: Full repository with non-main HEAD
- **WHEN** a run starts against a full native Project
- **THEN** the sandbox contains real Git history and the captured HEAD branch
- **AND** restricted views do not receive broader history

### Requirement: Automatic Git publication
The system SHALL automatically commit completed file changes and publish their
original objects using the run's fixed base and current publication authority.

#### Scenario: Completed writing turn
- **WHEN** an Agent completes a turn with changed knowledge files
- **THEN** its original commit OID becomes the cloud branch tip after publication
- **AND** the next run sees those files and history

#### Scenario: Concurrent branch advance or stop
- **WHEN** the base changes or the run loses its publication authority
- **THEN** the run cannot overwrite the branch
- **AND** its recovery checkpoint retains unpublished work

### Requirement: Git-aware recovery
The system SHALL preserve unpublished commits, the index and working-tree files
without treating Git metadata as knowledge content.

#### Scenario: Sandbox replacement
- **WHEN** a new execution restores an acknowledged checkpoint
- **THEN** original commits and acknowledged dirty files remain available
