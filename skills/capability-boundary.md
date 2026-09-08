# Capability boundary

An agent's reach is a closed world. It has:

- a shell;
- `gh` and a GitHub token, acting through Nate's identity; and
- the filesystem of the checkout it is working in.

It does not have:

- a browser session or application UIs;
- a credential store;
- account or billing settings;
- physical access; or
- an identity of its own — it acts through Nate's token.

Use this as the test for whether a step is agent work: does it require anything
outside that world? The named cases are examples, not an exhaustive checklist of
capabilities an agent might lack. A step that needs a capability outside the
boundary is outside the agent's reach even when it is not one of the examples
above.
