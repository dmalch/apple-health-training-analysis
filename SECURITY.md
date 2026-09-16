# Security

These are local scripts. They read an Apple Health export or an iPhone backup on
your own machine and write a DuckDB database next to them. Nothing is uploaded
anywhere, and there is no server, no telemetry and no network call in the analysis
path.

Two things are nevertheless worth care:

- **The backup password.** It is read from `$IOS_BACKUP_PASSWORD`, the macOS login
  keychain, or a password manager reference in your profile — never from a command
  line, where it would land in shell history and in the process table.
- **Your own data.** `data/` and `profiles/*.toml` are gitignored for a reason. A
  profile holds a max heart rate and a device UDID; the database holds everything
  your watch has ever recorded. Check `git status` before pushing a fork.

If you find a vulnerability, please open a GitHub issue. If it looks like something
that should not be public first, say so in the issue without the details and a
private channel can be arranged.
