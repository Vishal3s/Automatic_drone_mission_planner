"""
Fly a planned mission in the ArduCopter simulator that Mission Planner
installs (ArduPilot SITL), fully from the planner.

The planner stays the brain. This module only:
1. starts the simulator (ArduCopter.exe from Mission Planner's `sitl` folder)
   with a quadcopter at a home position next to the building,
2. connects to it (MAVLink, pymavlink -- already in requirements.txt),
3. uploads the planned mission, arms, switches to AUTO and starts it,
4. reports progress until the copter has landed again (or the time limit).

Mission Planner can watch the same simulated copter at the same time: the
simulator offers MAVLink on TCP 5760 (taken by the planner, and the
simulator waits for it before starting), 5762 and 5763. Mission Planner has
no command-line option to connect over TCP, so connect it once by hand:
TCP -> Connect -> host 127.0.0.1, port 5762.

Facts used (ArduPilot docs / Mission Planner docs):
- SITL serial ports: SERIAL0 tcp:5760 (waits for a client), SERIAL1 tcp:5762,
  SERIAL2 tcp:5763, all MAVLink.
- Mission Planner keeps the simulator in Documents\\Mission Planner\\sitl and
  its default parameters in ...\\sitl\\default_params\\copter.parm.
- Copter ignores NAV_WAYPOINT's yaw field; heading is set with
  CONDITION_YAW. DO/CONDITION commands run when the navigation command
  BEFORE them completes, so each inspection point is: fly to the point,
  then hold there (LOITER_TIME) while turning to face the wall
  (CONDITION_YAW), tilting the gimbal and taking the photo.
- ArduPilot keeps HOME as mission item 0, so a home placeholder is sent first.
"""

from __future__ import annotations

import glob
import logging
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from app.config import settings
from app.mission.schema import Mission, MissionCommand

log = logging.getLogger(__name__)

# MAVLink ids (common.xml)
CMD_NAV_WAYPOINT = 16
CMD_NAV_LOITER_TIME = 19
CMD_NAV_RETURN_TO_LAUNCH = 20
CMD_NAV_LAND = 21
CMD_NAV_TAKEOFF = 22
CMD_CONDITION_YAW = 115
CMD_DO_DIGICAM_CONTROL = 203
CMD_DO_MOUNT_CONTROL = 205
CMD_COMPONENT_ARM_DISARM = 400
CMD_MISSION_START = 300
FRAME_GLOBAL = 0
FRAME_MISSION = 2
FRAME_GLOBAL_RELATIVE_ALT = 3


class SimulatorError(RuntimeError):
    """Something the user can fix (simulator not found, no connection, arming refused...)."""


@dataclass
class Row:
    """One MAVLink mission item."""

    command: int
    frame: int
    params: list = field(default_factory=lambda: [0.0, 0.0, 0.0, 0.0])  # param1..4
    lat: float = 0.0
    lon: float = 0.0
    alt: float = 0.0


# ---------------------------------------------------------------------
# Mission -> ArduCopter mission items (pure, tested)
# ---------------------------------------------------------------------


def ardupilot_rows(mission: Mission, home: tuple[float, float, float]) -> list[Row]:
    """
    The mission as ArduCopter items, item 0 = home placeholder. Inspection
    waypoints (those with a camera heading) get a hold with yaw/gimbal/photo.
    """
    hold = float(settings.sitl_inspection_hold_s)
    rows = [Row(CMD_NAV_WAYPOINT, FRAME_GLOBAL, lat=home[0], lon=home[1], alt=home[2])]
    last = (home[0], home[1])
    for item in mission.mission_items:
        if item.command == MissionCommand.TAKEOFF:
            rows.append(Row(CMD_NAV_TAKEOFF, FRAME_GLOBAL_RELATIVE_ALT, alt=float(item.altitude or 0.0)))
        elif item.command in (MissionCommand.WAYPOINT, MissionCommand.LOITER):
            if item.latitude is None or item.longitude is None:
                raise SimulatorError(f"Item #{item.sequence} has no position.")
            alt = float(item.altitude or 0.0)
            if item.command == MissionCommand.LOITER:
                rows.append(Row(CMD_NAV_LOITER_TIME, FRAME_GLOBAL_RELATIVE_ALT, [float(item.hold_time_s or 0), 0, 0, 0],
                                item.latitude, item.longitude, alt))
            else:
                rows.append(Row(CMD_NAV_WAYPOINT, FRAME_GLOBAL_RELATIVE_ALT, lat=item.latitude, lon=item.longitude, alt=alt))
            inspect = item.yaw_deg is not None or item.gimbal_pitch_deg is not None or item.take_photo
            if inspect:
                # These run when the waypoint above is reached; the LOITER_TIME below keeps the copter there meanwhile.
                if item.yaw_deg is not None:
                    rows.append(Row(CMD_CONDITION_YAW, FRAME_MISSION, [float(item.yaw_deg), float(settings.sitl_yaw_rate_deg_s), 0, 0]))
                if item.gimbal_pitch_deg is not None:
                    rows.append(Row(CMD_DO_MOUNT_CONTROL, FRAME_MISSION, [float(item.gimbal_pitch_deg), 0, 0, 0], alt=2))  # param7 = 2: MAVLink targeting
                if item.take_photo:
                    rows.append(Row(CMD_DO_DIGICAM_CONTROL, FRAME_MISSION, [0, 0, 0, 0], lat=1))  # param5 = 1: shoot
                rows.append(Row(CMD_NAV_LOITER_TIME, FRAME_GLOBAL_RELATIVE_ALT, [hold, 0, 0, 0], item.latitude, item.longitude, alt))
            last = (item.latitude, item.longitude)
        elif item.command == MissionCommand.LAND:
            rows.append(Row(CMD_NAV_LAND, FRAME_GLOBAL_RELATIVE_ALT, lat=last[0], lon=last[1]))
        elif item.command == MissionCommand.RETURN_TO_LAUNCH:
            rows.append(Row(CMD_NAV_RETURN_TO_LAUNCH, FRAME_MISSION))
    return rows


def waypoints_text(rows: list[Row]) -> str:
    """
    The rows as a Mission Planner waypoint file ("QGC WPL 110", tab-separated):
    INDEX CURRENT FRAME COMMAND P1 P2 P3 P4 X/LAT Y/LON Z/ALT AUTOCONTINUE.
    Row 0 is home (CURRENT = 1), as Mission Planner writes it.
    """
    lines = ["QGC WPL 110"]
    for i, r in enumerate(rows):
        p = list(r.params) + [0.0] * (4 - len(r.params))
        lat = f"{r.lat:.8f}" if r.frame != FRAME_MISSION else f"{r.lat:g}"
        lon = f"{r.lon:.8f}" if r.frame != FRAME_MISSION else f"{r.lon:g}"
        lines.append("\t".join([str(i), "1" if i == 0 else "0", str(r.frame), str(r.command),
                                 *(f"{float(v):g}" for v in p[:4]), lat, lon, f"{float(r.alt):g}", "1"]))
    return "\n".join(lines) + "\n"


def save_waypoints(rows: list[Row], path: str | Path) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(waypoints_text(rows), encoding="utf-8")
    return target


def default_home(mission: Mission) -> tuple[float, float]:
    """Where the simulated copter starts when no home is given: under the first waypoint (outside the building)."""
    first = next((i for i in mission.mission_items if i.latitude is not None), None)
    if first is None:
        raise SimulatorError("The mission has no coordinates -- nowhere to start the simulator.")
    return first.latitude, first.longitude


# ---------------------------------------------------------------------
# Finding and starting the simulator
# ---------------------------------------------------------------------


def _sitl_dirs() -> list[Path]:
    """Where Mission Planner keeps its simulator. Windows often moves Documents into OneDrive, so both are checked."""
    if settings.ardupilot_sitl_dir:
        return [Path(settings.ardupilot_sitl_dir)]
    docs = []
    for base in (os.environ.get("OneDrive"), os.environ.get("OneDriveConsumer"), str(Path.home() / "OneDrive")):
        if base:
            docs.append(Path(base) / "Documents")
    docs.append(Path.home() / "Documents")
    seen, out = set(), []
    for d in docs:
        p = d / "Mission Planner" / "sitl"
        if str(p).lower() not in seen:
            seen.add(str(p).lower())
            out.append(p)
    return out


def find_simulator() -> tuple[Path, Path | None]:
    """(ArduCopter.exe, copter.parm or None). Raises SimulatorError with what to do when missing."""
    folders = _sitl_dirs()
    for folder in folders:
        exes = [e for e in sorted(glob.glob(str(folder / "**" / "ArduCopter*.exe"), recursive=True)) if Path(e).is_file()]
        if exes:
            break
    else:
        raise SimulatorError(
            "ArduCopter simulator not found in: " + "; ".join(str(f) for f in folders)
            + ". Open Mission Planner -> Simulation -> Multirotor -> Stable once (it downloads ArduCopter.exe), close it, "
              "then try again -- or set ARDUPILOT_SITL_DIR in .env to the folder that holds ArduCopter.exe."
        )
    parms = sorted(glob.glob(str(folder / "**" / "copter.parm"), recursive=True))
    return Path(exes[0]), (Path(parms[0]) if parms else None)


def start_simulator(home: tuple[float, float, float], *, heading_deg: float = 0.0) -> subprocess.Popen:
    exe, parm = find_simulator()
    workdir = Path(settings.sitl_work_dir)
    workdir.mkdir(parents=True, exist_ok=True)
    cmd = [str(exe), "--model", settings.sitl_model, "--home", f"{home[0]:.7f},{home[1]:.7f},{home[2]:g},{heading_deg:g}",
           "--speedup", str(settings.sitl_speedup), "-w"]
    if parm is not None:
        cmd += ["--defaults", str(parm)]
    else:
        log.warning("copter.parm not found next to the simulator -- starting without default parameters (arming may be refused).")
    log.info("Starting simulator: %s", " ".join(cmd))
    out = open(workdir / "sitl.log", "w", encoding="utf-8", errors="replace")
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.Popen(cmd, cwd=str(workdir), stdout=out, stderr=subprocess.STDOUT, creationflags=flags)


def open_mission_planner() -> bool:
    """Starts Mission Planner if it is installed (it is only a viewer here). False if not found."""
    candidates = [settings.mission_planner_exe] if settings.mission_planner_exe else []
    candidates += [r"C:\Program Files (x86)\Mission Planner\MissionPlanner.exe", r"C:\Program Files\Mission Planner\MissionPlanner.exe"]
    for path in candidates:
        if path and os.path.isfile(path):
            subprocess.Popen([path], cwd=str(Path(path).parent))
            return True
    return False


# ---------------------------------------------------------------------
# MAVLink link (pymavlink)
# ---------------------------------------------------------------------


class CopterLink:
    """The planner's connection to the simulated copter."""

    def __init__(self, address: str | None = None, *, master=None) -> None:
        self.address = address or settings.sitl_connection
        self.master = master
        self.status: list[str] = []

    def connect(self, timeout_s: float = 60.0) -> None:
        from pymavlink import mavutil

        deadline = time.time() + timeout_s
        last = None
        while self.master is None and time.time() < deadline:
            try:
                self.master = mavutil.mavlink_connection(self.address, source_system=255)
            except OSError as exc:  # simulator still starting
                last = exc
                time.sleep(1.0)
        if self.master is None:
            raise SimulatorError(f"Could not connect to the simulator at {self.address}: {last}")
        if self.master.wait_heartbeat(timeout=max(5.0, deadline - time.time())) is None:
            raise SimulatorError("Connected, but the simulator sent no heartbeat.")
        self.master.mav.request_data_stream_send(self.master.target_system, self.master.target_component, 0, 4, 1)

    def _note(self, msg) -> None:
        if msg is not None and msg.get_type() == "STATUSTEXT":
            text = msg.text if isinstance(msg.text, str) else msg.text.decode(errors="replace")
            self.status.append(text.strip("\x00 "))
            log.info("Copter: %s", self.status[-1])

    def wait_gps(self, timeout_s: float = 120.0) -> None:
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = self.master.recv_match(type=["GPS_RAW_INT", "STATUSTEXT"], blocking=True, timeout=2)
            self._note(msg)
            if msg is not None and msg.get_type() == "GPS_RAW_INT" and msg.fix_type >= 3:
                return
        raise SimulatorError("The simulated GPS never got a 3D fix.")

    def upload(self, rows: list[Row], timeout_s: float = 30.0) -> None:
        m = self.master
        ts, tc = m.target_system, m.target_component
        m.mav.mission_clear_all_send(ts, tc)
        m.mav.mission_count_send(ts, tc, len(rows))
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            msg = m.recv_match(type=["MISSION_REQUEST_INT", "MISSION_REQUEST", "MISSION_ACK", "STATUSTEXT"], blocking=True, timeout=5)
            self._note(msg)
            if msg is None or msg.get_type() == "STATUSTEXT":
                continue
            if msg.get_type() == "MISSION_ACK":
                if msg.type == 0:  # MAV_MISSION_ACCEPTED
                    return
                raise SimulatorError(f"The copter rejected the mission (MAV_MISSION_RESULT {msg.type}).")
            r = rows[msg.seq]
            p = list(r.params) + [0.0] * (4 - len(r.params))
            m.mav.mission_item_int_send(ts, tc, msg.seq, r.frame, r.command, 0, 1, p[0], p[1], p[2], p[3],
                                        int(round(r.lat * 1e7)) if r.frame != FRAME_MISSION else int(r.lat),
                                        int(round(r.lon * 1e7)) if r.frame != FRAME_MISSION else int(r.lon),
                                        float(r.alt))
        raise SimulatorError("Mission upload timed out.")

    def _set_mode(self, name: str) -> None:
        mapping = self.master.mode_mapping() or {}
        if name not in mapping:
            raise SimulatorError(f"Flight mode {name} not available.")
        self.master.set_mode(mapping[name])

    def arm_and_start(self, timeout_s: float = 120.0) -> None:
        """GUIDED -> arm (retried until the copter's pre-arm checks pass) -> AUTO -> start the mission."""
        m = self.master
        self._set_mode("GUIDED")
        deadline = time.time() + timeout_s
        while not m.motors_armed():
            if time.time() > deadline:
                why = [s for s in self.status if "PreArm" in s or "Arm" in s][-3:]
                raise SimulatorError("The copter refused to arm" + (f": {'; '.join(why)}" if why else "."))
            m.mav.command_long_send(m.target_system, m.target_component, CMD_COMPONENT_ARM_DISARM, 0, 1, 0, 0, 0, 0, 0, 0)
            end = time.time() + 3
            while time.time() < end:
                self._note(m.recv_match(type=["HEARTBEAT", "STATUSTEXT", "COMMAND_ACK"], blocking=True, timeout=1))
                if m.motors_armed():
                    break
        self._set_mode("AUTO")
        m.mav.command_long_send(m.target_system, m.target_component, CMD_MISSION_START, 0, 0, 0, 0, 0, 0, 0, 0)

    def monitor(self, total_items: int, timeout_s: float, on_progress=print) -> bool:
        """Until the copter has landed and disarmed again. True when it finished in time."""
        m = self.master
        deadline = time.time() + timeout_s
        last_seq = -1
        while time.time() < deadline:
            msg = m.recv_match(type=["MISSION_CURRENT", "HEARTBEAT", "STATUSTEXT", "GLOBAL_POSITION_INT"], blocking=True, timeout=2)
            self._note(msg)
            if msg is None:
                continue
            kind = msg.get_type()
            if kind == "MISSION_CURRENT" and msg.seq != last_seq:
                last_seq = msg.seq
                on_progress(f"Mission item {msg.seq}/{total_items - 1}")
            elif kind == "HEARTBEAT" and last_seq > 0 and not m.motors_armed():
                on_progress("Landed and disarmed -- mission finished.")
                return True
        return False

    def return_to_launch(self) -> None:
        try:
            self._set_mode("RTL")
        except Exception as exc:  # noqa: BLE001 -- best effort
            log.warning("RTL failed: %s", exc)
