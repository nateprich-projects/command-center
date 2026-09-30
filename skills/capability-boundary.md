# Capability boundary

Use this closed-world test for each concrete plan step, including how the step will
be verified. The world has three outcomes:

- **Workable by any agent.** The step needs only the shared agent capabilities: a
  shell, `gh` and a GitHub token acting through Nate's identity, and the filesystem
  of the checkout.
- **Workable only where the Claude Code environment is present.** The step needs
  the Mac mini outside the checkout, which Codex's sandbox cannot reach or verify
  and a Claude Code session on the Mac mini can. This is the middle case: it is
  agent work, but only Claude Code can take it. It covers, among others:
  - `~/.claude`, installed skills, routine prompts and the statusline;
  - live config edits, deploys, live runs, logs and journals, and the Muse and
    Codex session stores;
  - loading, reloading, booting out or kickstarting a LaunchAgent with
    `launchctl bootstrap`, `launchctl bootout`, `launchctl load` or
    `launchctl kickstart`. An acceptance criterion that a plist loads belongs
    here too, including both sides of a reinstall pair: booting out the old job
    and loading the replacement;
  - `sudo -n -u jeffy …` and
    `sudo -n -u _personalops /opt/personal-ops/bin/personal-ops-admin …`, under
    the sudoers rules already installed;
  - reading a Keychain item into a process, never printing it;
  - `osascript`, `gh`, and the funnel's own commands.
- **Workable by no agent.** The step needs Nate himself:
  - typing in a secret or token;
  - creating an account, an app or a tunnel;
  - a browser or device sign-in, such as `muse login` or `gh auth refresh`;
  - a GUI consent prompt, such as Keychain's Always Allow, Full Disk Access, or
    anything in System Settings;
  - billing;
  - running root code from a user-writable path, such as an `install.sh` in his
    home folder;
  - hands on hardware (physical access);
  - a decision.

**Physical access means hands on the hardware**, not "runs on the Mac mini": a step
that only has to run on the Mac mini is Claude Code environment work.

The named cases are examples, not an exhaustive checklist of capabilities an agent
might lack. A step that needs anything outside the relevant world is outside that
agent's reach even when it is not one of the examples above.

**One human action per ticket.** A step list never mixes session work with Nate's:
file the session steps as `claude-code-environment` and give Nate only what is his.
