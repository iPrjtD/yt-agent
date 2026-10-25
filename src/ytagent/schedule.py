"""Windows Task Scheduler registration for the autopilot (runs as the current user, only while logged on)."""

from __future__ import annotations

import subprocess
import sys
import tempfile
from datetime import date
from pathlib import Path
from xml.sax.saxutils import escape

from ytagent.config import ROOT, get_settings
from ytagent.proc import NO_WINDOW

FOLDER = "Buzzbee"

_TASK = """<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{description}</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>{repetition}
    </CalendarTrigger>
  </Triggers>
  <Principals><Principal id="Author"><LogonType>InteractiveToken</LogonType>
    <RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>true</RunOnlyIfNetworkAvailable>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
    <Hidden>true</Hidden>
    <Enabled>true</Enabled>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{command}</Command>
      <Arguments>{arguments}</Arguments>
      <WorkingDirectory>{workdir}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _pythonw() -> Path:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return candidate if candidate.exists() else exe


def tasks() -> list[dict]:
    cfg = get_settings().autopilot
    today = date.today().isoformat()
    every = f"""
      <Repetition><Interval>PT{cfg.ideas_every_hours}H</Interval><Duration>P1D</Duration></Repetition>"""
    out = [{"name": "Ideas", "job": "ideas", "start": f"{today}T00:15:00", "repetition": every,
            "description": f"Buzzbee: collect and score ideas every {cfg.ideas_every_hours} h"}]
    for i, at in enumerate(cfg.produce_at, 1):
        out.append({"name": f"Produce {i}", "job": "produce", "start": f"{today}T{at}:00", "repetition": "",
                    "description": f"Buzzbee: make one Short at {at}"})
    out.append({"name": "Cleanup", "job": "cleanup", "start": f"{today}T{cfg.cleanup_at}:00", "repetition": "",
                "description": "Buzzbee: clear old cache files"})
    out.append({"name": "Review", "job": "review", "start": f"{today}T00:05:00",
                "repetition": """
      <Repetition><Interval>PT10M</Interval><Duration>P1D</Duration></Repetition>""",
                "description": "Buzzbee: publish Shorts you approved in YouTube Studio (every 10 min)"})
    return out


def install() -> list[str]:
    done = []
    for t in tasks():
        xml = _TASK.format(description=escape(t["description"]), start=t["start"], repetition=t["repetition"],
                           command=escape(str(_pythonw())), arguments=f"-m ytagent.cli autopilot run {t['job']}",
                           workdir=escape(str(ROOT)))
        with tempfile.NamedTemporaryFile("w", suffix=".xml", delete=False, encoding="utf-16") as fh:
            fh.write(xml)
            xml_path = fh.name
        name = f"{FOLDER}\\{t['name']}"
        out = subprocess.run(["schtasks", "/create", "/tn", name, "/xml", xml_path, "/f"], capture_output=True,
                             text=True, creationflags=NO_WINDOW)
        Path(xml_path).unlink(missing_ok=True)
        if out.returncode != 0:
            raise RuntimeError(f"schtasks failed for {name}: {out.stderr or out.stdout}")
        done.append(f"{name}: {t['description']}")
    return done


def uninstall() -> list[str]:
    removed = []
    for t in tasks():
        name = f"{FOLDER}\\{t['name']}"
        out = subprocess.run(["schtasks", "/delete", "/tn", name, "/f"], capture_output=True, text=True,
                             creationflags=NO_WINDOW)
        if out.returncode == 0:
            removed.append(name)
    return removed


def status() -> list[tuple[str, str, str]]:
    rows = []
    for t in tasks():
        name = f"{FOLDER}\\{t['name']}"
        out = subprocess.run(["schtasks", "/query", "/tn", name, "/fo", "list", "/v"], capture_output=True,
                             text=True, creationflags=NO_WINDOW)
        info = dict(line.split(":", 1) for line in out.stdout.splitlines() if ":" in line)
        rows.append((name, info.get("Next Run Time", "not installed").strip(), info.get("Last Result", "").strip()))
    return rows
