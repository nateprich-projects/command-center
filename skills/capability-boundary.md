# Capability boundary

Use this closed-world test for each concrete plan step, including how the step will
be verified. The world has three outcomes:

- **Workable by any agent.** The step needs only the shared agent capabilities: a
  shell, `gh` and a GitHub token acting through Nate's identity, and the filesystem
  of the checkout.
- **Workable only where the Claude Code environment is present.** The step needs a
  machine-local Claude Code surface that Codex cannot reach or verify, such as
  `~/.claude`, installed skills, routine prompts, or the statusline. This is the
  middle case: it is agent work, but only Claude Code can take it.
- **Workable by no agent.** The step needs a capability outside both agent
  environments, such as a browser session or application UI, a credential store,
  account or billing settings, physical access, or an identity of its own.

The last category's named cases are examples, not an exhaustive checklist of
capabilities an agent might lack. A step that needs anything outside the relevant
world is outside that agent's reach even when it is not one of the examples above.
