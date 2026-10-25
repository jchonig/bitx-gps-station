import json
import socket
import subprocess
import threading

import pytest

import gps_station as gs

# ---------------------------------------------------------------- grid


@pytest.mark.parametrize(
    "lat, lon, chars, grid",
    [
        (41.714775, -72.727260, 6, "FN31pr"),   # ARRL HQ, Newington CT
        (41.714775, -72.727260, 4, "FN31"),
        (51.477, -0.001, 6, "IO91xl"),           # just west of Greenwich
        (-33.8568, 151.2153, 6, "QF56od"),       # Sydney Opera House
        (0.0, 0.0, 6, "JJ00aa"),
        (-90.0, -180.0, 6, "AA00aa"),
    ],
)
def test_maidenhead(lat, lon, chars, grid):
    assert gs.maidenhead(lat, lon, chars) == grid


@pytest.mark.parametrize(
    "text, default, expect",
    [
        ("10.0.0.5:9000", 8081, ("10.0.0.5", 9000)),
        ("10.0.0.5", 8081, ("10.0.0.5", 8081)),
        ("radio.local", 2947, ("radio.local", 2947)),
        ("radio.local:2948", 2947, ("radio.local", 2948)),
    ],
)
def test_hostport(text, default, expect):
    assert gs.hostport(text, default) == expect


# ---------------------------------------------------------------- radio

ZBITXRE_GREETING = "FREQ 14074000\nMODE USB\nMYGRID FN31pr\n"
SBITX_GREETING = "\x1b[2J\x1b[Hsbitx v3.1\r\n"


@pytest.mark.parametrize(
    "greeting, kind",
    [
        (ZBITXRE_GREETING, "zbitxre"),
        (SBITX_GREETING, "sbitx"),
        ("Welcome to sBitx v2.0\n", "sbitx"),
        ("SSH-2.0-OpenSSH_9.2\r\n", None),
        ("", None),
    ],
)
def test_which_radio(greeting, kind):
    assert gs.which_radio(greeting) == kind


def test_zbitxre_grid():
    assert gs._zbitxre_grid(ZBITXRE_GREETING) == "FN31pr"
    assert gs._zbitxre_grid("FREQ 7074000\n") is None


class FakeZbitxre:
    """Stands in for _session against a zbitxre radio."""

    def __init__(self, grid, refuse=False):
        self.grid = grid
        self.refuse = refuse
        self.sent = []

    def __call__(self, addr, send=None, listen=1.5):
        answer = ""
        if send is not None:
            self.sent.append(send)
            if self.refuse:
                answer = "ERROR bad grid\n"
            else:
                self.grid = send.split()[1]
        return f"FREQ 7074000\nMODE LSB\nMYGRID {self.grid}\n", answer


class FakeSbitx:
    def __init__(self, grid):
        self.grid = grid
        self.sent = []

    def __call__(self, addr, send=None, listen=1.5):
        answer = ""
        if send is not None:
            self.sent.append(send)
            cmd, _, arg = send.partition(" ")
            if cmd == "grid":
                self.grid = arg
            elif cmd == "?MYGRID":
                answer = f"MYGRID {self.grid}\r\n"
        return SBITX_GREETING, answer


def test_set_grid_zbitxre(monkeypatch):
    radio = FakeZbitxre("FN31pr")
    monkeypatch.setattr(gs, "_session", radio)
    assert gs.set_radio_grid(None, "FN42aa")
    assert radio.sent == ["MYGRID FN42aa"]
    assert radio.grid == "FN42aa"


def test_set_grid_zbitxre_already_set(monkeypatch):
    radio = FakeZbitxre("FN42aa")
    monkeypatch.setattr(gs, "_session", radio)
    assert gs.set_radio_grid(None, "FN42aa")
    assert radio.sent == []


def test_set_grid_zbitxre_refused(monkeypatch):
    radio = FakeZbitxre("FN31pr", refuse=True)
    monkeypatch.setattr(gs, "_session", radio)
    assert not gs.set_radio_grid(None, "FN42aa")


def test_set_grid_sbitx(monkeypatch):
    radio = FakeSbitx("FN31pr")
    monkeypatch.setattr(gs, "_session", radio)
    assert gs.set_radio_grid(None, "FN42aa")
    assert radio.sent == ["grid FN42aa", "?MYGRID"]


def test_set_grid_unknown(monkeypatch):
    monkeypatch.setattr(gs, "_session", lambda addr, send=None, listen=1.5: ("hello\n", ""))
    assert not gs.set_radio_grid(None, "FN42aa")


def test_session_over_tcp():
    """_session against a real socket: greeting first, then one command and its answer."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    got = []

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.sendall(ZBITXRE_GREETING.encode())
            got.append(conn.recv(100))
            conn.sendall(b"OK\n")

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    try:
        greeting, answer = gs._session(srv.getsockname(), "MYGRID FN42aa", listen=0.3)
    finally:
        t.join(5)
        srv.close()
    assert greeting == ZBITXRE_GREETING
    assert answer == "OK\n"
    assert got == [b"MYGRID FN42aa\n"]


# ---------------------------------------------------------------- gpsd


def test_gpsd_poll_returns_last_fix():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    msgs = [
        {"class": "VERSION", "release": "3.22"},
        {"class": "TPV", "mode": 1},
        {"class": "TPV", "mode": 3, "lat": 10.0, "lon": 20.0},
        {"class": "SKY", "nSat": 3, "uSat": 1, "satellites": [
            {"ss": 31.0, "used": True}, {"ss": 22.0}, {"ss": 0.0}]},
        {"class": "TPV", "mode": 2, "lat": 41.714775, "lon": -72.72726},
    ]

    def serve():
        conn, _ = srv.accept()
        with conn:
            conn.recv(100)
            conn.sendall(b"".join(json.dumps(m).encode() + b"\n" for m in msgs) + b"not json\n")
            conn.recv(100)  # hold the connection open until the client closes

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    g = gs.Gpsd(srv.getsockname())
    try:
        fix = None
        for _ in range(20):
            fix = g.poll(0.2)
            if fix:
                break
    finally:
        g.sock.close()
        t.join(5)
        srv.close()
    assert fix == (41.714775, -72.72726)
    assert (g.mode, g.seen, g.heard, g.used, g.best) == (2, 3, 2, 1, 31.0)


def test_gpsd_toff_unit():
    g = gs.Gpsd(None)
    for shm, unit in (("NTP-1", -1), ("NTP0", 0), ("NTP2", 2)):
        g.buf = b""
        g.sock = FakeSock(json.dumps({"class": "TOFF", "shm": shm}).encode() + b"\n")
        g.poll(0)
        assert g.shm_unit == unit


class FakeSock:
    def __init__(self, data):
        self.data = data

    def recv(self, n):
        data, self.data = self.data, b""
        return data


@pytest.fixture(autouse=True)
def _select_ready(monkeypatch):
    real = gs.select.select

    def select(r, w, x, t):
        if r and isinstance(r[0], FakeSock):
            return r, [], []
        return real(r, w, x, t)
    monkeypatch.setattr(gs.select, "select", select)


def test_warns_once_when_gpsd_has_no_shm(ntp, monkeypatch, capsys):
    monkeypatch.setattr(ntp.shm, "active_unit", lambda: None)
    ntp.tick(-1)
    ntp.tick(-1)
    assert capsys.readouterr().out.count("isn't writing time to shared memory") == 1
    ntp.tick(0)
    ntp.tick(-1)
    assert capsys.readouterr().out.count("isn't writing time to shared memory") == 1


def test_gpsd_sky_from_satellite_list():
    g = gs.Gpsd(None)
    g._sky({"class": "SKY", "satellites": [{"used": True, "ss": 30}, {"ss": 0}, {}]})
    assert (g.seen, g.heard, g.used, g.best) == (3, 1, 1, 30)
    g._sky({"class": "SKY"})                     # partial SKY: keep what we had
    assert (g.seen, g.heard, g.used, g.best) == (3, 1, 1, 30)


# ---------------------------------------------------------------- ntp.conf

BASE_CONF = "driftfile /var/lib/ntpsec/ntp.drift\npool 2.debian.pool.ntp.org iburst\n"


def test_write_and_read_block():
    text = gs.write_block(BASE_CONF, 2, -0.125, False)
    assert text.startswith(BASE_CONF)
    assert gs.read_block(text) == (2, -0.125, False)
    assert not gs.foreign_shm(text)

    again = gs.write_block(text, 0, 0.5, True)
    assert again.count(gs.BEGIN) == 1
    assert gs.read_block(again) == (0, 0.5, True)


def test_write_block_adds_newline():
    text = gs.write_block("pool x iburst", 0, 0.0, False)
    assert text.startswith("pool x iburst\n" + gs.BEGIN)


def test_read_block_absent():
    assert gs.read_block(BASE_CONF) is None


def test_foreign_shm():
    assert gs.foreign_shm(BASE_CONF + "refclock shm unit 0 refid GPS\n")
    assert not gs.foreign_shm(BASE_CONF + "# refclock shm unit 0\n")


NTPQ = """\
     remote           refid      st t when poll reach   delay   offset   jitter
=======================================================================================================
 SHM(0)          .GPS.            0 l    3   64  377   0.0000 -112.345   2.1000
*192.0.2.10      .PPS.            1 u   33   64  377  12.3400   0.1230   0.4560
+198.51.100.7    192.0.2.10       2 u   40   64  177  20.0000  -1.0000   0.9000
"""


def test_parse_ntpq():
    rows = gs.parse_ntpq(NTPQ)
    assert rows == [
        (" ", "SHM(0)", "377", -112.345),
        ("*", "192.0.2.10", "377", 0.123),
        ("+", "198.51.100.7", "177", -1.0),
    ]


def test_is_shm():
    assert gs.is_shm("SHM(2)")
    assert gs.is_shm("127.127.28.0")
    assert not gs.is_shm("192.0.2.10")


# ---------------------------------------------------------------- shared memory


def test_active_unit(monkeypatch):
    w = gs.ShmWatch()
    counts = {0: 5, 2: None, 4: 7, 6: 1}
    monkeypatch.setattr(w, "_count", lambda unit: counts[unit])
    assert w.active_unit() is None          # first look only records counts
    assert w.active_unit() is None          # nothing moved
    counts[4] = 8
    counts[6] = 2
    assert w.active_unit() == 4             # lowest moving unit wins
    assert w.active_unit() is None


# ---------------------------------------------------------------- Ntp.tick


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@pytest.fixture
def ntp(tmp_path, monkeypatch):
    conf = tmp_path / "ntp.conf"
    conf.write_text(BASE_CONF)
    clock = Clock()
    monkeypatch.setattr(gs.time, "monotonic", clock)
    n = gs.Ntp(str(conf), "ntpsec", dry_run=True)
    n.clock = clock
    return n


def test_tick_adds_refclock_then_calibrates(ntp, monkeypatch):
    unit = [None]
    monkeypatch.setattr(ntp.shm, "active_unit", lambda: unit[0])
    monkeypatch.setattr(gs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=NTPQ))

    ntp.tick()
    assert ntp.virtual is None              # no fix yet, nothing written

    unit[0] = 0
    ntp.clock.now += gs.SHM_CHECK_S
    ntp.tick()
    assert gs.read_block(ntp.virtual) == (0, 0.0, False)

    # calibration waits 15 minutes, then takes CAL_SAMPLES samples
    for _ in range(gs.CAL_SAMPLES):
        ntp.clock.now += 15 * 60
        ntp.tick()
    unit_, time1, calibrated = gs.read_block(ntp.virtual)
    assert (unit_, calibrated) == (0, True)
    assert time1 == pytest.approx(0.112345, abs=1e-4)

    # calibrated: left alone from then on
    before = ntp.virtual
    ntp.clock.now += 15 * 60
    ntp.tick()
    assert ntp.virtual == before


def test_tick_no_calibration_without_network(ntp, monkeypatch):
    monkeypatch.setattr(ntp.shm, "active_unit", lambda: 0)
    no_net = NTPQ.replace("*192.0.2.10", " 192.0.2.10")
    monkeypatch.setattr(gs.subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(
        a, 0, stdout=no_net))
    ntp.tick()
    for _ in range(gs.CAL_SAMPLES * 2):
        ntp.clock.now += 15 * 60
        ntp.tick()
    assert gs.read_block(ntp.virtual) == (0, 0.0, False)


def test_tick_leaves_foreign_refclock_alone(ntp, monkeypatch, tmp_path):
    (tmp_path / "ntp.conf").write_text(BASE_CONF + "refclock shm unit 0 refid GPS\n")
    monkeypatch.setattr(ntp.shm, "active_unit", lambda: 0)
    ntp.tick()
    assert ntp.gave_up
    assert ntp.virtual is None


def test_tick_missing_conf(tmp_path):
    n = gs.Ntp(str(tmp_path / "missing.conf"), "ntpsec", dry_run=True)
    n.tick()
    assert n.gave_up


def test_write_for_real(tmp_path, monkeypatch):
    conf = tmp_path / "ntp.conf"
    conf.write_text(BASE_CONF)
    conf.chmod(0o644)
    calls = []
    monkeypatch.setattr(gs.subprocess, "run", lambda args, **k: calls.append(args))
    n = gs.Ntp(str(conf), "ntpsec", dry_run=False)
    n._write(gs.write_block(BASE_CONF, 2, 0.0, False), "test")
    assert gs.read_block(conf.read_text()) == (2, 0.0, False)
    assert conf.stat().st_mode & 0o777 == 0o644
    assert calls == [["systemctl", "restart", "ntpsec"]]
