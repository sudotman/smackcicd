# Running as a service

```bash
smackcicd service install      # install and start
smackcicd service restart      # after editing the config
smackcicd service uninstall
smackcicd service show         # print what install would register
```

## Windows

`install` registers a **scheduled task** that runs `pythonw.exe -m smackcicd
--home <home> watch`. Specifically:

- **No console window.** Under `python.exe` the daemon owns a console, and
  closing it kills the runner. `pythonw.exe` has none.
- **Runs without anyone logged on** (S4U logon), from boot.
- **A watchdog trigger every five minutes**, with "ignore new instances". It
  does nothing while the daemon runs, and relaunches it if it ever stops.
  Task Scheduler's own restart settings only cover a task that fails to
  *start*, not one that exits later.
- **Child processes open no windows either**, so no stray console can be
  closed in the middle of a build.

Registering a task that runs without a logon needs an **elevated** shell
(Run as administrator). Without admin rights, `--interactive` registers a task
that runs only while you are logged on.

S4U tasks have no network credentials, so they cannot reach Windows file
shares. smackcicd does not need any: git and the forge are HTTP or SSH.

The interpreter is the one smackcicd is installed into (`sys.executable`).
Python's `py.exe` launcher and the Microsoft Store `python.exe` alias are
never registered.

## Linux

`install` writes a **systemd user unit** to
`~/.config/systemd/user/smackcicd.service` with `Restart=always`, then
enables and starts it. To keep it running while you are logged out, and to
start it at boot, run once:

```bash
loginctl enable-linger $USER
```

For a system-wide service, use `smackcicd service show` to print the unit, add
`User=` and install it under `/etc/systemd/system`.

## Several runners on one machine

Give each its own home folder and `[service] name`, and its own
`[server] port`.

## Logs

- `logs/smackcicd.log`: the daemon log, rotated at 8 MB, 5 files kept. A
  crash is logged here too.
- `logs/<tag>/<platform>-uat.log`: each build's full UAT output.
- `logs/<tag>/<platform>-build.log`: the runner's own lines for that build.
