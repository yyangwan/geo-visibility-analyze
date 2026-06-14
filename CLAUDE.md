
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

- **Plan**: `docs/user-like-llm-collection-issue-list.md`
- **Phase**: Issue 2.3 (Kimi 多轮 search) Complete — 14 tests passing
- **Next**: Issue 3.3 (解析降级与错误记录集成) - 添加 parse_error 字段支持
- **Progress**: Response parser (37 tests ✅) → Kimi multi-round (14 tests ✅) → Parse error handling

## Session Handoff Protocol

Use `/checkpoint` skill for saving and resuming work state:
- User says "save progress" → invoke `/checkpoint`
- User says "resume" → invoke `/checkpoint resume`
