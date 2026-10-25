import json
import socket
import threading

import pytest

import gps_station_apps as ga

# ---------------------------------------------------------------- helpers


def test_grids_match():
    assert ga.grids_match("FN31pr", "fn31PR")
    assert ga.grids_match("FN31", "FN31pr")
    assert ga.grids_match("FN31pr", "FN31")
    assert not ga.grids_match("FN31pr", "FN31ps")
    assert not ga.grids_match("FN31", "FN32")
    assert not ga.grids_match(None, "FN31")


def test_running(tmp_path):
    def proc(pid, argv):
        d = tmp_path / str(pid)
        d.mkdir()
        (d / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    proc(10, ["/usr/bin/wsjtx"])
    proc(11, ["/opt/GridTracker2/gridtracker2", "--no-sandbox"])
    proc(12, ["JS8Call"])
    (tmp_path / "13").mkdir()                     # kernel thread: no cmdline file
    (tmp_path / "self").mkdir()
    assert ga.running(str(tmp_path)) == {"wsjtx": 10, "gridtracker2": 11, "js8call": 12}


INI = """\
[General]
Configuration\\TCPServerPort=0
TCPEnabled=false

[Configuration]
TCPEnabled=true
TCPServerPort=2442
UDPServer="239.255.0.1"
"""


def test_ini_value(tmp_path):
    p = tmp_path / "x.ini"
    p.write_text(INI)
    assert ga.ini_value(str(p), "TCPServerPort") == "2442"
    assert ga.ini_true(str(p), "TCPEnabled")
    assert ga.ini_value(str(p), "UDPServer") == "239.255.0.1"
    assert ga.ini_value(str(p), "Missing") is None
    assert ga.ini_value(str(tmp_path / "none.ini"), "TCPEnabled") is None


def test_settings(tmp_path):
    p = tmp_path / "gps-station" / "apps.json"
    s = ga.Settings(str(p))
    assert not s.is_ticked("wsjtx")
    s.set_ticked("wsjtx", True)
    assert json.loads(p.read_text()) == {"ticked": {"wsjtx": True}}
    assert ga.Settings(str(p)).is_ticked("wsjtx")


def test_gps_grid_steady():
    g = ga.GpsGrid(("127.0.0.1", 1), steady=60)
    g.feed((41.714775, -72.72726), 0)
    assert g.grid is None
    g.feed((41.714775, -72.72726), 59)
    assert g.grid is None
    g.feed((41.714775, -72.72726), 60)
    assert g.grid == "FN31pr"
    g.feed((42.5, -71.5), 70)                     # moved; not steady yet
    assert g.grid == "FN31pr" and g.candidate == "FN42gm"
    g.feed((42.5, -71.5), 130)
    assert g.grid == "FN42gm"
    assert g.has_fix(140) and not g.has_fix(200)


def test_receiver_text():
    g = ga.GpsGrid(("127.0.0.1", 1))
    assert g.receiver() == "gpsd not reachable"
    g.gpsd.sock = object()
    assert g.receiver() == "waiting for gpsd"
    g.gpsd.mode, g.gpsd.seen, g.gpsd.heard, g.gpsd.used, g.gpsd.best = 1, 6, 0, 0, 0
    assert g.receiver() == "no fix: 0 used, 0 heard of 6 in view"
    g.gpsd.heard, g.gpsd.best = 1, 22.0
    assert g.receiver() == "no fix: 0 used, 1 heard of 6 in view (best 22 dB-Hz)"
    g.gpsd.mode, g.gpsd.seen, g.gpsd.used, g.gpsd.heard, g.gpsd.best = 3, 9, 7, 8, 41.0
    assert g.receiver() == "3D fix: 7 used, 8 heard of 9 in view (best 41 dB-Hz)"


# ---------------------------------------------------------------- WSJT-X protocol


def test_status_roundtrip():
    assert ga.decode(ga.encode_status("WSJT-X", "FN31pr")) == (ga.STATUS, 2, "WSJT-X", "FN31pr")


def test_location_encoding():
    msg = ga.encode_location("WSJT-X", "FN42aa", schema=3)
    assert msg[:12] == bytes.fromhex("adbccbda000000030000000b")
    assert ga.decode(msg) == (ga.LOCATION, 3, "WSJT-X", None)
    assert msg.endswith(b"\x00\x00\x00\x06FN42aa")


def test_decode_rejects_junk():
    assert ga.decode(b"hello") is None
    assert ga.decode(b"\xad\xbc\xcb\xda\x00") is None


def free_udp_port():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(cond, tick, tries=50):
    for _ in range(tries):
        tick()
        if cond():
            return True
        threading.Event().wait(0.02)
    return False


def write_ini(tmp_path, name, **values):
    p = tmp_path / name
    p.write_text("[Configuration]\n" + "".join(f"{k}={v}\n" for k, v in values.items()))
    return str(p)


@pytest.fixture
def wsjtx(tmp_path):
    port = free_udp_port()
    ini = write_ini(tmp_path, "WSJT-X.ini", UDPServer="127.0.0.1", UDPServerPort=port,
                    AcceptUDPRequests="true", AutoGrid="true")
    listener = ga.WsjtxListener()
    app = ga.Wsjtx(listener, ini=ini)
    radio = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)    # plays WSJT-X
    radio.bind(("127.0.0.1", 0))
    radio.settimeout(2)
    yield app, listener, radio, port
    listener.close()
    radio.close()


def test_wsjtx_read_and_set(wsjtx):
    app, listener, fake, port = wsjtx
    assert app.present({"wsjtx": 42}) == 42
    app.tick()
    grid, why = app.read()
    assert grid is None and isinstance(why, ga.Waiting)

    fake.sendto(ga.encode_status("WSJT-X", "AA11aa"), ("127.0.0.1", port))
    assert wait_for(lambda: app.read()[0] == "AA11aa", app.tick)

    assert app.write("FN31pr") is None
    data, _ = fake.recvfrom(1024)
    assert ga.decode(data) == (ga.LOCATION, 2, "WSJT-X", None)
    assert data.endswith(b"FN31pr")


def test_jtdx_shares_the_listener(wsjtx, tmp_path):
    app, listener, fake, port = wsjtx
    jtdx = ga.Jtdx(listener, ini=app.ini)
    jtdx.present({"jtdx": 7})
    jtdx.tick()                                   # binds the port
    fake.sendto(ga.encode_status("JTDX", "FN42aa"), ("127.0.0.1", port))
    assert wait_for(lambda: jtdx.read()[0] == "FN42aa", jtdx.tick)
    assert app.read()[0] is None                  # nothing from WSJT-X itself
    assert "can't be changed" in jtdx.write("FN31pr")


def test_wsjtx_write_needs_settings(wsjtx, tmp_path):
    app, listener, fake, port = wsjtx
    app.ini = write_ini(tmp_path, "off.ini", UDPServer="127.0.0.1", UDPServerPort=port,
                        AcceptUDPRequests="false", AutoGrid="true")
    assert "Accept UDP requests" in app.write("FN31pr")
    app.ini = write_ini(tmp_path, "off2.ini", UDPServer="127.0.0.1", UDPServerPort=port,
                        AcceptUDPRequests="true", AutoGrid="false")
    assert "AutoGrid" in app.write("FN31pr")


def test_wsjtx_port_taken(tmp_path):
    other = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)       # plays GridTracker
    other.bind(("127.0.0.1", 0))
    port = other.getsockname()[1]
    try:
        ini = write_ini(tmp_path, "WSJT-X.ini", UDPServer="127.0.0.1", UDPServerPort=port)
        app = ga.Wsjtx(ga.WsjtxListener(), ini=ini)
        app.present({"wsjtx": 1})
        app.tick()
        grid, why = app.read()
        assert grid is None and "multicast" in why
    finally:
        other.close()


def test_wsjtx_multicast_on_loopback(tmp_path):
    """WSJT-X sending to a multicast group out of the loopback interface, with
    another listener (GridTracker) in the same group: both hear it."""
    port, group = free_udp_port(), "239.255.0.1"
    ini = write_ini(tmp_path, "WSJT-X.ini", UDPServer=group, UDPServerPort=port)
    app = ga.Wsjtx(ga.WsjtxListener(), ini=ini)
    app.present({"wsjtx": 1})
    app.tick()
    other = ga.WsjtxListener()                    # plays GridTracker
    other.ensure(group, port)
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton("127.0.0.1"))
    sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    try:
        assert app.listener.why is None and other.why is None
        sender.sendto(ga.encode_status("WSJT-X", "FN31pr"), (group, port))
        assert wait_for(lambda: app.read()[0] == "FN31pr", app.tick)
        assert wait_for(lambda: "wsjtx" in other.seen, other.poll)
    finally:
        sender.close()
        other.close()
        app.listener.close()


# ---------------------------------------------------------------- JS8Call


class FakeJs8:
    def __init__(self, grid):
        self.grid = grid
        self.srv = socket.socket()
        self.srv.bind(("127.0.0.1", 0))
        self.srv.listen(4)
        self.port = self.srv.getsockname()[1]
        self.got = []
        threading.Thread(target=self.serve, daemon=True).start()

    def serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            with conn:
                msg = json.loads(conn.makefile().readline())
                self.got.append(msg)
                if msg["type"] == "STATION.GET_GRID":
                    conn.sendall(b'{"type":"RIG.FREQ","value":""}\n')
                    reply = {"type": "STATION.GRID", "value": self.grid, "params": {}}
                    conn.sendall(json.dumps(reply).encode() + b"\n")
                elif msg["type"] == "STATION.SET_GRID":
                    self.grid = msg["value"]


def test_js8call(tmp_path):
    fake = FakeJs8("AA11aa")
    ini = write_ini(tmp_path, "JS8Call.ini", TCPEnabled="true", AcceptTCPRequests="true",
                    TCPServer="127.0.0.1", TCPServerPort=fake.port)
    app = ga.Js8Call(ini=ini)
    assert app.read() == ("AA11aa", None)
    assert app.write("FN31pr") is None
    assert wait_for(lambda: fake.grid == "FN31pr", lambda: None)
    assert app.read() == ("FN31pr", None)
    fake.srv.close()


def test_js8call_api_off(tmp_path):
    app = ga.Js8Call(ini=write_ini(tmp_path, "JS8Call.ini", TCPEnabled="false"))
    grid, why = app.read()
    assert grid is None and "TCP server API" in why
    assert "TCP server API" in app.write("FN31pr")
    app.ini = write_ini(tmp_path, "b.ini", TCPEnabled="true", AcceptTCPRequests="false")
    assert "Accept TCP requests" in app.write("FN31pr")


# ---------------------------------------------------------------- radio


def test_radio_read_zbitxre(monkeypatch):
    monkeypatch.setattr(ga.gs, "_session", lambda addr, send=None, listen=1.5:
                        ("FREQ 7074000\nMYGRID FN31pr\n", ""))
    r = ga.Radio(("127.0.0.1", 1))
    assert r.read() == ("FN31pr", None)
    assert r.label() == "Radio (zbitxre)"


def test_radio_read_sbitx(monkeypatch):
    def session(addr, send=None, listen=1.5):
        return "\x1b[Hsbitx v3\r\n", ("MYGRID FN31pr\r\n" if send == "?MYGRID" else "")
    monkeypatch.setattr(ga.gs, "_session", session)
    assert ga.Radio(("127.0.0.1", 1)).read() == ("FN31pr", None)


# ---------------------------------------------------------------- tracker


class FakeApp(ga.App):
    settable = True

    def __init__(self, key, grid, settable=True, why=None):
        self.key = self.title = key
        self.procs = (key,)
        self.grid = grid
        self.settable = settable
        self.why = why
        self.sent = []
        self.takes = True

    def read(self):
        return self.grid, (None if self.grid else self.why)

    def write(self, grid):
        if not self.settable:
            return "can't"
        self.sent.append(grid)
        if self.takes:
            self.grid = grid
        return None


class Clock:
    now = 0.0

    def __call__(self):
        return self.now


@pytest.fixture
def setup(tmp_path):
    settings = ga.Settings(str(tmp_path / "apps.json"))
    told = []
    clock = Clock()

    def make(*apps):
        return ga.Tracker(list(apps), settings, notify=lambda t, x: told.append((t, x)),
                          clock=clock)
    return make, settings, told, clock


def status(t, key):
    return t.row(key).status


def test_unticked_is_shown_not_changed(setup):
    make, settings, told, clock = setup
    app = FakeApp("a", "AA11aa")
    t = make(app)
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.STALE
    assert app.sent == [] and told == []


def test_ticked_stale_is_updated(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "AA11aa")
    t = make(app)
    t.update("FN31pr", {"a": 1})
    assert app.sent == ["FN31pr"]
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MATCH


def test_hand_set_grid_is_left_until_forced(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "FN31pr")
    t = make(app)
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MATCH
    app.grid = "FN42aa"                           # typed in by hand
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MANUAL
    assert app.sent == []
    t.force("a")
    assert app.sent == ["FN31pr"]
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MATCH


def test_moving_clears_hand_set(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "FN31pr")
    t = make(app)
    t.update("FN31pr", {"a": 1})
    app.grid = "FN42aa"
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MANUAL
    t.update("FN31ps", {"a": 1})                  # the GPS moved
    assert app.sent == ["FN31ps"]


def test_restart_resets_hand_set(setup):
    make, settings, told, clock = setup
    app = FakeApp("a", "FN31pr")
    t = make(app)
    t.update("FN31pr", {"a": 1})
    app.grid = "FN42aa"
    t.update("FN31pr", {"a": 1})
    assert status(t, "a") == ga.MANUAL
    t.update("FN31pr", {})
    assert status(t, "a") == ga.STOPPED
    t.update("FN31pr", {"a": 2})
    assert status(t, "a") == ga.STALE


def test_cant_set_notifies_once(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "AA11aa", settable=False)
    t = make(app)
    t.update("FN31pr", {"a": 1})
    t.update("FN31pr", {"a": 1})
    assert len(told) == 1
    assert told[0] == ("a has AA11aa; GPS grid is FN31pr", "can't")
    assert t.row("a").why == "can't"              # the reason stays on the row
    t.update("FN31pr", {})                        # quit and started again
    t.update("FN31pr", {"a": 2})
    assert len(told) == 2


def test_update_that_does_not_take(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "AA11aa")
    app.takes = False
    t = make(app)
    t.update("FN31pr", {"a": 1})
    assert app.sent == ["FN31pr"]
    clock.now += 10
    t.update("FN31pr", {"a": 1})
    assert app.sent == ["FN31pr"] and told == []  # still waiting to see it
    clock.now += ga.VERIFY_S
    t.update("FN31pr", {"a": 1})
    assert len(told) == 1 and "didn't take" in told[0][1]
    clock.now += 100
    t.update("FN31pr", {"a": 1})
    assert app.sent == ["FN31pr"] and len(told) == 1


def test_unreadable_ticked_notifies_but_waiting_does_not(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    settings.set_ticked("b", True)
    a = FakeApp("a", None, why="can't read it")
    b = FakeApp("b", None, why=ga.Waiting("waiting"))
    t = make(a, b)
    t.update("FN31pr", {"a": 1, "b": 2})
    assert status(t, "a") == ga.UNKNOWN and status(t, "b") == ga.UNKNOWN
    assert [x for _, x in told] == ["can't read it"]


def test_no_fix_compares_nothing(setup):
    make, settings, told, clock = setup
    settings.set_ticked("a", True)
    app = FakeApp("a", "AA11aa")
    t = make(app)
    t.update(None, {"a": 1})
    assert app.sent == [] and t.row("a").writable is False
    assert status(t, "a") == ga.NOGPS and t.row("a").grid == "AA11aa"


def test_snapshot(setup):
    make, settings, told, clock = setup
    t = make(FakeApp("a", "AA11aa"), ga.GridTracker())
    t.update("FN31pr", {"a": 1, "gridtracker2": 3})
    snap = {r["key"]: r for r in t.snapshot()}
    assert snap["a"]["status"] == ga.STALE and snap["a"]["writable"]
    assert snap["gridtracker"]["tickable"] is False
    assert snap["gridtracker"]["why"] == "follows WSJT-X / JTDX"
