
## Skill routing

When the user's request matches an available skill, invoke it via the Skill tool. When in doubt, invoke the skill.

Key routing rules:
- Product ideas/brainstorming → invoke /office-hours
- Strategy/scope → invoke /plan-ceo-review
- Architecture → invoke /plan-eng-review
- Design system/plan review → invoke /design-consultation or /plan-design-review
- Full review pipeline → invoke /autoplan
- Bugs/errors → invoke /investigate
- QA/testing site behavior → invoke /qa or /qa-only
- Code review/diff check → invoke /review
- Visual polish → invoke /design-review
- Ship/deploy/PR → invoke /ship or /land-and-deploy
- Save progress → invoke /context-save
- Resume context → invoke /context-restore

## Current Status

- **Progress**: `~/.gstack/projects/geo-visibility-analyze/progress.md`
- **Plan**: `~/.gstack/projects/geo-visibility-analyze/implementation-plan-20260510.md`
- **Phase**: Eng Review完成 — 架构已锁定，9个决策已解决
- **Next**: Phase 1 实现 — Data Foundation (3条并行lane: models+migration, source extraction, adapter search mode)
