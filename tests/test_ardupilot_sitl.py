"""
ArduCopter simulator link -- no simulator needed: the mission conversion is
pure, and the upload handshake runs against a fake pymavlink connection.
"""

from __future__ import annotations

import types

import pytest

from app.mission.schema import Mission, MissionCommand, MissionItem
from app.sim.ardupilot_sitl import (
    CMD_CONDITION_YAW,
    CMD_DO_DIGICAM_CONTROL,
    CMD_DO_MOUNT_CONTROL,
    CMD_NAV_LOITER_TIME,
    CMD_NAV_RETURN_TO_LAUNCH,
    CMD_NAV_TAKEOFF,
    CMD_NAV_WAYPOINT,
    CopterLink,
    SimulatorError,
    ardupilot_rows,
    default_home,
    waypoints_text,
)

HOME = (13.0808, 80.2716, 10.0)


def _mission(camera=True):
    wp = dict(yaw_deg=180.0, gimbal_pitch_deg=-30.0, take_photo=True) if camera else {}
    return Mission(mission_name="t", mission_items=[
        MissionItem(sequence=1, command=MissionCommand.TAKEOFF, altitude=40),
        MissionItem(sequence=2, command=MissionCommand.WAYPOINT, latitude=13.0810, longitude=80.2716, altitude=40, **wp),
        MissionItem(sequence=3, command=MissionCommand.RETURN_TO_LAUNCH),
    ])


def test_home_first_then_takeoff_and_inspection_hold():
    rows = ardupilot_rows(_mission(), HOME)
    assert [r.command for r in rows] == [
        CMD_NAV_WAYPOINT,          # 0 = home placeholder (ArduPilot keeps home at item 0)
        CMD_NAV_TAKEOFF,
        CMD_NAV_WAYPOINT,          # fly to the inspection point
        CMD_CONDITION_YAW,         # then, on arrival: face the wall
        CMD_DO_MOUNT_CONTROL,      #   tilt the gimbal
        CMD_DO_DIGICAM_CONTROL,    #   take the photo
        CMD_NAV_LOITER_TIME,       #   while holding there
        CMD_NAV_RETURN_TO_LAUNCH,
    ]
    assert (rows[0].lat, rows[0].lon, rows[0].alt) == HOME
    assert rows[1].alt == 40
    assert rows[3].params[0] == 180.0 and rows[3].params[3] == 0      # absolute heading
    assert rows[4].params[0] == -30.0 and rows[4].alt == 2            # pitch, param7 = MAVLink targeting
    assert rows[5].lat == 1                                           # param5 = shoot
    assert rows[6].lat == 13.0810 and rows[6].params[0] > 0           # hold at the same point


def test_plain_waypoint_has_no_extra_items():
    rows = ardupilot_rows(_mission(camera=False), HOME)
    assert [r.command for r in rows] == [CMD_NAV_WAYPOINT, CMD_NAV_TAKEOFF, CMD_NAV_WAYPOINT, CMD_NAV_RETURN_TO_LAUNCH]


def test_default_home_is_under_the_first_waypoint():
    assert default_home(_mission()) == (13.0810, 80.2716)


class _Msg(types.SimpleNamespace):
    def get_type(self):
        return self.kind


class _FakeMaster:
    """Plays the copter's side of the MAVLink mission upload."""

    def __init__(self, n_accept=True):
        self.target_system, self.target_component = 1, 1
        self.sent = []
        self.queue = []
        self.accept = n_accept
        outer = self

        class _Mav:
            def mission_clear_all_send(self, *a):
                pass

            def mission_count_send(self, ts, tc, count):
                outer.count = count
                outer.queue = [_Msg(kind="MISSION_REQUEST_INT", seq=i) for i in range(count)]
                outer.queue.append(_Msg(kind="MISSION_ACK", type=0 if outer.accept else 1))

            def mission_item_int_send(self, *args):
                outer.sent.append(args)

        self.mav = _Mav()

    def recv_match(self, **kw):
        return self.queue.pop(0) if self.queue else None


def test_upload_answers_every_request_and_scales_positions():
    master = _FakeMaster()
    rows = ardupilot_rows(_mission(), HOME)
    CopterLink(master=master).upload(rows, timeout_s=5)
    assert len(master.sent) == len(rows)
    seq, frame, cmd = master.sent[2][2], master.sent[2][3], master.sent[2][4]
    assert (seq, cmd) == (2, CMD_NAV_WAYPOINT)
    assert master.sent[2][11] == int(round(13.0810 * 1e7))            # x = latitude * 1e7
    assert master.sent[5][11] == 1                                     # DIGICAM param5 unscaled


def test_rejected_upload_is_reported():
    with pytest.raises(SimulatorError):
        CopterLink(master=_FakeMaster(n_accept=False)).upload(ardupilot_rows(_mission(), HOME), timeout_s=5)


def test_waypoint_file_is_mission_planner_format():
    text = waypoints_text(ardupilot_rows(_mission(), HOME))
    lines = text.strip().split("\n")
    assert lines[0] == "QGC WPL 110"
    home = lines[1].split("\t")
    assert len(home) == 12 and home[:4] == ["0", "1", "0", "16"] and float(home[8]) == HOME[0] and home[11] == "1"
    takeoff = lines[2].split("\t")
    assert takeoff[3] == "22" and takeoff[2] == "3" and float(takeoff[10]) == 40
    digicam = lines[6].split("\t")
    assert digicam[3] == "203" and digicam[8] == "1"     # param5 = shoot
    assert len(lines) == 1 + len(ardupilot_rows(_mission(), HOME))
