# Capability boundary

Use this closed-world test for each concrete plan step, including how the step will
be verified. The world has three outcomes:

- **Workable by any agent.** The step needs only the shared agent capabilities: a
  shell, `gh` and a GitHub token acting through Nate's identity, and the filesystem
  of the checkout. That includes code changes, `gh` and the funnel's own commands,
  `gh`-only readbacks, live runs, reads of logs, journals and session stores
  (including the Muse and Codex session stores), and corpus replays.
- **Workable only where the Claude Code environment is present.** The step needs
  the Mac mini outside the checkout, which Codex's sandbox cannot reach or verify
  and a Claude Code session on the Mac mini can. This is the middle case: it is
  agent work, but only Claude Code can take it. It covers, among others:
  - live config edits and deploys;
  - `~/.claude`, installed skills, routine prompts and the statusline;
  - loading, reloading, booting out or kickstarting a LaunchAgent with
    `launchctl bootstrap`, `launchctl bootout`, `launchctl load` or
    `launchctl kickstart`. An acceptance criterion that a plist loads belongs
    here too, including both sides of a reinstall pair: booting out the old job
    and loading the replacement;
  - `sudo -n -u jeffy …` and
    `sudo -n -u _personalops /opt/personal-ops/bin/personal-ops-admin …`, under
    the sudoers rules already installed;
  - Keychain reads, taking an item into a process without printing it;
  - Desktop scheduled tasks;
  - `osascript`;
  - anything needing a Claude Code cloud thread.
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

**Needs records capability, never reservation.** `claude-code-environment` says the
step needs the Mac mini; it never reserves taken work for a Claude session. A
session reserves work by claiming it. A step verified by `gh`-only readbacks is not
Mac work.

**One human action per ticket.** A step list never mixes session work with Nate's:
file the session steps as `claude-code-environment` and give Nate only what is his.
