# SPDX-License-Identifier: GPL-3.0-or-later
"""Running the daemon in the background: a Windows scheduled task, or systemd.

Windows: a scheduled task that starts at boot and has a second trigger every
five minutes. With "ignore new instances" that trigger does nothing while the
daemon runs and relaunches it if it ever stops -- task-level restart settings
only cover a task that fails to *start*. It runs under pythonw.exe, so there
is no console window for anyone to close.

Linux: a systemd user unit with Restart=always.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from .util import NO_WINDOW


class ServiceError(RuntimeError):
    pass


def interpreter(windowless=True):
    """This interpreter -- the one smackcicd is installed into -- as a path."""
    python = Path(sys.executable)
    if windowless and os.name == "nt":
        pythonw = python.with_name("pythonw.exe")
        if pythonw.exists():
            return pythonw
    return python


def watch_args(cfg):
    return ["-m", "smackcicd", "--home", str(cfg.home), "watch"]


# ------------------------------------------------------------------ Windows
def _ps_quote(text):
    return "'%s'" % str(text).replace("'", "''")


def windows_script(cfg, interactive=False, user=None):
    """The PowerShell 5.1-compatible script that registers the task.

    Kept free of ``?.``, ``??`` and the ternary: Windows still ships 5.1. The
    account comes from WindowsIdentity, never ``$env:USERDOMAIN\\$env:USERNAME``
    (that is WORKGROUP\\name on a machine outside a domain, which is no SID).
    """
    name = cfg.get("service.name") or "smackcicd"
    arguments = " ".join('"%s"' % a if " " in a else a for a in watch_args(cfg))
    logon = "Interactive" if interactive else "S4U"
    user_expr = _ps_quote(user) if user else "[Security.Principal.WindowsIdentity]::GetCurrent().Name"
    lines = [
        "$ErrorActionPreference = 'Stop'",
        "$name = %s" % _ps_quote(name),
        "$user = %s" % user_expr,
        "$action = New-ScheduledTaskAction -Execute %s -Argument %s -WorkingDirectory %s"
        % (_ps_quote(interpreter()), _ps_quote(arguments), _ps_quote(cfg.home)),
        "$watchdog = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) "
        "-RepetitionInterval (New-TimeSpan -Minutes 5)",
        "$triggers = @((New-ScheduledTaskTrigger -AtStartup), $watchdog)",
    ]
    if interactive:
        lines.append("$triggers = $triggers + @(New-ScheduledTaskTrigger -AtLogOn -User $user)")
    lines += [
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries "
        "-DontStopIfGoingOnBatteries -StartWhenAvailable -RestartInterval "
        "(New-TimeSpan -Minutes 2) -RestartCount 999 -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-MultipleInstances IgnoreNew",
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType %s -RunLevel Highest"
        % logon,
        "if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {",
        "    Unregister-ScheduledTask -TaskName $name -Confirm:$false",
        "}",
        "Register-ScheduledTask -TaskName $name -Action $action -Trigger $triggers "
        "-Principal $principal -Settings $settings "
        "-Description 'smackcicd: builds Unreal Engine tags' | Out-Null",
        "Start-ScheduledTask -TaskName $name",
        "Write-Output (\"installed and started '\" + $name + \"' as \" + $user)",
    ]
    return "\r\n".join(lines) + "\r\n"


def _run_powershell(script):
    with tempfile.NamedTemporaryFile("w", suffix=".ps1", delete=False, encoding="utf-8-sig") as f:
        f.write(script)
        path = f.name
    try:
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", path], capture_output=True, text=True, creationflags=NO_WINDOW)
    finally:
        os.unlink(path)
    output = (result.stdout + result.stderr).strip()
    if result.returncode != 0:
        if "Access is denied" in output or "0x80070005" in output:
            output += ("\n\nRegistering a task that runs without anyone logged on needs an "
                       "elevated (Run as administrator) shell.")
        raise ServiceError(output or "powershell exited %d" % result.returncode)
    return output


def _windows_stop_script(cfg, remove):
    name = cfg.get("service.name") or "smackcicd"
    home = str(cfg.home).replace("'", "''")
    lines = [
        "$name = %s" % _ps_quote(name),
        "if (Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue) {",
        "    Stop-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue",
    ]
    if remove:
        lines.append("    Unregister-ScheduledTask -TaskName $name -Confirm:$false")
    lines += [
        "}",
        # Stopping the task does not stop the process it started.
        "Get-CimInstance Win32_Process -Filter \"Name='pythonw.exe' OR Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*smackcicd*watch*' -and "
        "$_.CommandLine -like '*%s*' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force }"
        % home,
        "Write-Output 'stopped'",
    ]
    return "\r\n".join(lines) + "\r\n"


# --------------------------------------------------------------------- systemd
def systemd_unit(cfg):
    command = " ".join([str(interpreter(windowless=False))] + ['"%s"' % a if " " in a else a
                                                                for a in watch_args(cfg)])
    return "\n".join([
        "[Unit]",
        "Description=smackcicd -- tag-triggered Unreal Engine builds",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "ExecStart=%s" % command,
        "WorkingDirectory=%s" % cfg.home,
        "Restart=always",
        "RestartSec=30",
        "Environment=PYTHONUNBUFFERED=1",
        "",
        "[Install]",
        "WantedBy=default.target",
        "",
    ])


def _unit_path(cfg):
    name = cfg.get("service.name") or "smackcicd"
    return Path.home() / ".config" / "systemd" / "user" / ("%s.service" % name)


def _systemctl(*args):
    result = subprocess.run(["systemctl", "--user"] + list(args), capture_output=True, text=True)
    if result.returncode != 0:
        raise ServiceError((result.stdout + result.stderr).strip()
                           or "systemctl exited %d" % result.returncode)
    return result.stdout.strip()


# ------------------------------------------------------------------- entry points
def install(cfg, interactive=False, user=None):
    if os.name == "nt":
        return _run_powershell(windows_script(cfg, interactive=interactive, user=user))
    if sys.platform.startswith("linux"):
        unit = _unit_path(cfg)
        unit.parent.mkdir(parents=True, exist_ok=True)
        unit.write_text(systemd_unit(cfg), encoding="utf-8")
        _systemctl("daemon-reload")
        _systemctl("enable", "--now", unit.name)
        return ("installed %s and started it.\nTo keep it running when you are logged out "
                "(and start it at boot), run once:  loginctl enable-linger %s"
                % (unit, os.environ.get("USER", "$USER")))
    raise ServiceError("service install supports Windows and Linux; on other systems run "
                       "`smackcicd watch` under your own process supervisor")


def uninstall(cfg):
    if os.name == "nt":
        return _run_powershell(_windows_stop_script(cfg, remove=True))
    unit = _unit_path(cfg)
    if unit.exists():
        _systemctl("disable", "--now", unit.name)
        unit.unlink()
        _systemctl("daemon-reload")
        return "removed %s" % unit
    return "no unit installed at %s" % unit


def restart(cfg):
    name = cfg.get("service.name") or "smackcicd"
    if os.name == "nt":
        _run_powershell(_windows_stop_script(cfg, remove=False))
        return _run_powershell("Start-ScheduledTask -TaskName %s\r\nWrite-Output 'started'\r\n"
                               % _ps_quote(name))
    return _systemctl("restart", _unit_path(cfg).name) or "restarted"
