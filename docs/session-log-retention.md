# Agent session-log retention

How the Muse and Codex session logs are kept on the Mac mini (#2013), and how to check that
they still work. The logs are compressed on the internal drive and then moved to the external
SSD. Nothing is deleted unless a verified, readable copy exists first.

## Retention windows

| Store | Internal path | Kept raw | Compressed in place | Moved to the SSD |
| --- | --- | --- | --- | --- |
| Muse journals | `~/.local/share/muse/sessions` | The current and previous provider weekly windows (`session_log_compress.muse_compress_before`: `usage.muse_window_start(now) - usage.SEVEN_DAY`) | Anything older | Every `.gz` |
| Codex rollouts | `~/.codex/sessions` | The last 14 days (`CODEX_RAW_DAYS`), and every rollout whose `thread_source` is `user` or unknown | Older non-user rollouts | `.gz` rollouts whose `thread_source` is `automation`, `subagent` or `review` |
| Claude transcripts | `~/.claude/projects` | All of them | None | None |

The pacing meter in `usage.py` reads the raw Muse journals for the current and previous weekly
windows, so those two windows never leave the internal drive. The compression pass skips files
that are open (`lsof`) and files that change while they are being compressed. It is safe to
run it again.

Codex threads Nate started interactively stay internal, in both raw and `.gz` form, so app
history and resume keep working. The 2026-10-03 archive pass retained 23 of them.

## Layout and who runs what

- **Compression** runs from the launchd run-keeper (`scripts/run-keeper` calls
  `session_log_compress.py`). Launchd can write the internal drive.
- **The SSD archive** runs from the funnel watch's app-hosted check-in
  (`python3 session_log_archive.py`). It runs once per local day, on the first call at or after
  03:00. The day is stamped on the volume as `.last-daily-pass`. Launchd gets
  `Operation not permitted` on the external volume, so the run-keeper must never call it.
- **The archive root** is `/Volumes/External SSD/Archives/session-logs/<tool>/`, which mirrors
  each store's own tree (`muse/YYYY/MM/DD/<session>/session.jsonl.gz`,
  `codex/YYYY/MM/DD/rollout-….jsonl.gz`). `COMMAND_CENTER_AGENT_LOG_ARCHIVE_ROOT` overrides the
  base; unset it to return to the default. The path is machine-local and stays out of Git.
- **Each move is verified.** The copy is hashed and test-read before the internal file is
  removed. A failure keeps the source and counts it under `errors`. If the volume is
  unmounted, the pass touches nothing, prints `skipped; external volume is not mounted`, and
  does not stamp the day, so the next call tries again.
- **Readers** (`session_logs`) look in both the internal store and the archive, and read `.gz`
  transparently.

## Free-disk monitoring

There is no new monitor. #2012 explicitly declined to build a free-disk check (in the doctor,
the run-keeper sentinel or the brief), so the one canonical check is the funnel watch's
check-in. Each run reads `df -h /System/Volumes/Data`, logs free space on #684, and acts below
20 GB. Claude temp files and Colima disks live on the same external SSD
(`/Volumes/ClaudeScratch`), so that check also confirms the volume is mounted.

## Verification

Run these on the Mac mini as `nateprich`:

```sh
python3 usage.py muse                                   # pacing meter reads the live store; exit 0
zgrep -c . <a Muse session.jsonl.gz>                    # internal or archived sample; exit 0
gzcat <a Muse session.jsonl.gz> | head -1 | jq -c keys  # frame keys
gzcat <an archived Codex rollout .gz> | head -1 | jq -c .type   # "session_meta"
du -sk ~/.local/share/muse/sessions ~/.codex/sessions "/Volumes/External SSD/Archives/session-logs"/*
python3 -m pytest
```

Results from the first live pass (#2229, 2026-10-03) and this check (#2077, 2026-10-03) are on
#2077.
