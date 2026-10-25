"""gps_station_apps.py - keep the grid square right in running ham programs.

The logic behind gps-station-desktop, kept free of GTK so it can be tested.

Every few seconds it looks at which supported programs are running, reads
the grid each one is using, and compares it with the grid from the GPS.
Each program's row is then one of:

  match    the program has the GPS grid
  stale    it differs and has not matched since the program started (or
           since the GPS moved to a new square)
  manual   it matched, then changed while the GPS stayed put: somebody
           typed a grid into the program
  unknown  the program can't report its grid
  stopped  the program isn't running
  nogps    there is no GPS grid yet to compare with

A ticked program in the stale state is updated. A manual grid is left
alone until the GPS moves to another square or the user forces an update.
If a ticked program can't be updated, the caller is told once per launch
so it can notify the user.

Programs that can be read or changed while running:

  radio    sbitx / zbitxre over TCP 8081 (as gps_station.py does)
  WSJT-X   UDP: grid from its Status messages; set with a Location message
           (needs "Accept UDP requests" and AutoGrid)
  JTDX     UDP: grid from its Status messages; no way to set it
  JS8Call  TCP API: STATION.GET_GRID and STATION.SET_GRID
  fldigi, QSSTV   no way to read or set the operator's locator
  GridTracker     follows WSJT-X/JTDX; shown for information
"""
import errno
import ipaddress
import json
import math
import os
import re
import socket
import struct
import subprocess
import time

import gps_station as gs

MATCH, STALE, MANUAL, UNKNOWN, STOPPED, NOGPS = (
    "match", "stale", "manual", "unknown", "stopped", "nogps")

VERIFY_S = 30             # how long an update has to show up before it counts as failed
GRID_RE = re.compile(r"\b([A-R]{2}[0-9]{2}(?:[A-X]{2})?)\b", re.I)


def log(msg):
    gs.log(msg)


class Waiting(str):
    """A reason a grid can't be read yet that should clear up by itself."""


def grids_match(a, b):
    """Same square, allowing one of them to be the 4-character form of the other."""
    if not a or not b:
        return False
    a, b = a.lower(), b.lower()
    return a.startswith(b) if len(a) >= len(b) else b.startswith(a)


INTERPRETERS = {"sh", "bash", "dash", "python3", "python", "perl"}


def running(proc="/proc"):
    """Map of running programs' names (basename of argv[0], lower case) to a pid.
    For a script run by an interpreter, the script's name is listed too."""
    found = {}
    try:
        pids = os.listdir(proc)
    except OSError:
        return found
    for pid in pids:
        if not pid.isdigit():
            continue
        try:
            with open(os.path.join(proc, pid, "cmdline"), "rb") as f:
                argv = f.read().decode("utf-8", "replace").split("\0")
        except OSError:
            continue
        names = [os.path.basename(argv[0]).lower()]
        if names[0] in INTERPRETERS and len(argv) > 1 and not argv[1].startswith("-"):
            names.append(os.path.basename(argv[1]).lower())
        for name in names:
            if name:
                found.setdefault(name, int(pid))
    return found


def ini_value(path, key, section="Configuration"):
    """A value from a Qt .ini file, preferring the given section. None if absent."""
    found, current = None, None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if line.startswith("[") and line.endswith("]"):
                    current = line[1:-1]
                    continue
                name, sep, value = line.partition("=")
                if not sep or name.strip() != key:
                    continue
                value = value.strip().strip('"')
                if current == section:
                    return value
                if found is None:
                    found = value
    except OSError:
        return None
    return found


def ini_true(path, key):
    return (ini_value(path, key) or "").lower() == "true"


def config_dir():
    return os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")


# ---------------------------------------------------------------- programs

class App:
    """One program. Subclasses say how to find, read and set it."""

    key = ""
    title = ""
    procs = ()
    tickable = True           # False for rows shown only for information
    settable = False          # True when write() can change it while running

    def present(self, procs):
        """The pid it runs as, or None."""
        for name in self.procs:
            if name in procs:
                return procs[name]
        return None

    def read(self):
        """(grid, why): the grid it is using, or None and why it can't be read."""
        return None, f"{self.title} can't report its grid while running"

    def write(self, grid):
        """Send the grid. None if sent, else why it can't be."""
        return f"{self.title} can't be changed while running; set it in its settings"

    def hint(self):
        """What to check when an update was sent but didn't take."""
        return f"{self.title} didn't take the new grid"

    def tick(self):
        """Called every cycle, for programs that listen in the background."""


class Radio(App):
    key = "radio"
    title = "Radio"
    settable = True

    def __init__(self, addr):
        self.addr = addr
        self.kind = None

    def present(self, procs):
        try:
            socket.create_connection(self.addr, timeout=1).close()
            return -1
        except OSError:
            return None

    def read(self):
        try:
            greeting, _ = gs._session(self.addr)
            self.kind = gs.which_radio(greeting)
            if self.kind == "zbitxre":
                return gs._zbitxre_grid(greeting), None
            if self.kind == "sbitx":
                _, answer = gs._session(self.addr, "?MYGRID")
                m = GRID_RE.search(answer)
                return (m.group(1) if m else None), None
        except OSError as e:
            return None, f"radio: {e}"
        return None, "something answers on the radio's port but it isn't a radio I know"

    def write(self, grid):
        try:
            return None if gs.set_radio_grid(self.addr, grid) else "the radio didn't take it"
        except OSError as e:
            return f"radio: {e}"

    def label(self):
        return f"Radio ({self.kind})" if self.kind else self.title


class Js8Call(App):
    key = "js8call"
    title = "JS8Call"
    settable = True
    procs = ("js8call",)

    def __init__(self, ini=None):
        self.ini = ini or os.path.join(config_dir(), "JS8Call.ini")

    def _addr(self):
        host = ini_value(self.ini, "TCPServer") or "127.0.0.1"
        port = ini_value(self.ini, "TCPServerPort") or "2442"
        return host, int(port) if port.isdigit() else 2442

    def _ask(self, msg, want):
        with socket.create_connection(self._addr(), timeout=2) as s:
            s.sendall(json.dumps(msg).encode() + b"\n")
            if want is None:
                return None
            buf = b""
            end = time.monotonic() + 2
            while time.monotonic() < end:
                chunk = s.recv(65536)
                if not chunk:
                    break
                buf += chunk
                *lines, buf = buf.split(b"\n")
                for line in lines:
                    try:
                        reply = json.loads(line)
                    except ValueError:
                        continue
                    if reply.get("type") == want:
                        return reply.get("value")
        return None

    def read(self):
        if not ini_true(self.ini, "TCPEnabled"):
            return None, "turn on JS8Call's TCP server API (Settings, Reporting)"
        try:
            grid = self._ask({"type": "STATION.GET_GRID", "value": "", "params": {}},
                             "STATION.GRID")
        except OSError as e:
            return None, f"JS8Call API: {e}"
        return (grid or None), (None if grid else "JS8Call didn't answer")

    def write(self, grid):
        if not ini_true(self.ini, "TCPEnabled"):
            return "turn on JS8Call's TCP server API (Settings, Reporting)"
        if not ini_true(self.ini, "AcceptTCPRequests"):
            return "turn on Accept TCP requests in JS8Call (Settings, Reporting)"
        try:
            self._ask({"type": "STATION.SET_GRID", "value": grid, "params": {}}, None)
        except OSError as e:
            return f"JS8Call API: {e}"
        return None

    def hint(self):
        return "JS8Call didn't take the new grid; check AutoGrid in its settings"


# ---------------------------------------------------------------- WSJT-X protocol

MAGIC = 0xADBCCBDA
HEARTBEAT, STATUS, LOCATION = 0, 1, 11


def _qstr(s):
    if s is None:
        return struct.pack(">I", 0xFFFFFFFF)
    b = s.encode()
    return struct.pack(">I", len(b)) + b


class _Reader:
    def __init__(self, data):
        self.data, self.pos = data, 0

    def take(self, fmt):
        (v,) = struct.unpack_from(fmt, self.data, self.pos)
        self.pos += struct.calcsize(fmt)
        return v

    def utf8(self):
        n = self.take(">I")
        if n == 0xFFFFFFFF:
            return None
        s = self.data[self.pos:self.pos + n].decode("utf-8", "replace")
        self.pos += n
        return s


def encode_location(ident, grid, schema=2):
    return struct.pack(">III", MAGIC, schema, LOCATION) + _qstr(ident) + _qstr(grid)


def encode_status(ident, de_grid, schema=2):
    """A Status message as WSJT-X sends it, up to the fields we read (for tests)."""
    return (struct.pack(">III", MAGIC, schema, STATUS) + _qstr(ident)
            + struct.pack(">Q", 14074000) + _qstr("FT8") + _qstr("") + _qstr("")
            + _qstr("FT8") + struct.pack(">???II", False, False, False, 1500, 1500)
            + _qstr("N0CALL") + _qstr(de_grid) + _qstr(""))


def decode(data):
    """(type, schema, id, de_grid) of a WSJT-X message, or None if it isn't one.
    de_grid is set for Status messages only."""
    try:
        r = _Reader(data)
        if r.take(">I") != MAGIC:
            return None
        schema, kind, ident = r.take(">I"), r.take(">I"), r.utf8()
        grid = None
        if kind == STATUS:
            r.take(">Q")
            for _ in range(4):                      # mode, DX call, report, tx mode
                r.utf8()
            r.take(">?"), r.take(">?"), r.take(">?")
            r.take(">I"), r.take(">I")
            r.utf8()                                # DE call
            grid = r.utf8()
        return kind, schema, ident, grid
    except (struct.error, IndexError):
        return None


def _join_everywhere(s, group):
    """Join a multicast group on every interface: WSJT-X is often set to send
    on loopback only, which a join on the default interface would miss."""
    joined = 0
    for index, _ in socket.if_nameindex():
        mreqn = socket.inet_aton(group) + socket.inet_aton("0.0.0.0") + struct.pack("=i", index)
        try:
            s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreqn)
            joined += 1
        except OSError:
            pass
    if not joined:
        mreq = socket.inet_aton(group) + socket.inet_aton("0.0.0.0")
        s.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, mreq)


class WsjtxListener:
    """Hears WSJT-X and JTDX on the UDP address they are set to send to."""

    def __init__(self):
        self.sock = None
        self.bound = None
        self.why = None
        self.seen = {}            # app key -> (grid, sender, schema, ident)

    def ensure(self, host, port):
        if self.bound == (host, port) and (self.sock or self.why):
            return
        self.close()
        self.bound = (host, port)
        try:
            multicast = ipaddress.ip_address(host).is_multicast
        except ValueError:
            multicast = False
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            if multicast:
                # others (GridTracker) can listen to the same group
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                s.bind(("", port))
                _join_everywhere(s, host)
            else:
                # no address reuse: on unicast it would steal datagrams from
                # whichever program already listens there
                s.bind((host, port))
        except OSError as e:
            s.close()
            if e.errno == errno.EADDRINUSE:
                self.why = (f"another program (GridTracker?) has UDP {host}:{port}; "
                            "set WSJT-X and it to a multicast address such as 239.255.0.1")
            else:
                self.why = f"UDP {host}:{port}: {e}"
            return
        s.setblocking(False)
        self.sock, self.why = s, None

    def close(self):
        if self.sock:
            self.sock.close()
        self.sock, self.bound, self.why = None, None, None

    def poll(self):
        while self.sock:
            try:
                data, sender = self.sock.recvfrom(65536)
            except (BlockingIOError, InterruptedError):
                return
            except OSError:
                return
            msg = decode(data)
            if not msg:
                continue
            kind, schema, ident, grid = msg
            key = "jtdx" if (ident or "").lower().startswith("jtdx") else "wsjtx"
            old = self.seen.get(key)
            if kind == STATUS:
                self.seen[key] = (grid, sender, schema, ident)
            elif old:
                self.seen[key] = (old[0], sender, schema, ident)
            else:
                self.seen[key] = (None, sender, schema, ident)

    def send_location(self, key, grid):
        grid_, sender, schema, ident = self.seen[key]
        self.sock.sendto(encode_location(ident, grid, min(schema, 3)), sender)


class Wsjtx(App):
    key = "wsjtx"
    title = "WSJT-X"
    settable = True
    procs = ("wsjtx",)
    ini_name = "WSJT-X.ini"

    def __init__(self, listener, ini=None):
        self.listener = listener
        self.ini = ini or os.path.join(config_dir(), self.ini_name)
        self.pid = None

    def _target(self):
        host = ini_value(self.ini, "UDPServer") or "127.0.0.1"
        port = ini_value(self.ini, "UDPServerPort") or "2237"
        return host, int(port) if port.isdigit() else 2237

    def present(self, procs):
        pid = super().present(procs)
        if pid != self.pid:
            self.listener.seen.pop(self.key, None)     # a new run reports afresh
            self.pid = pid
        return pid

    def tick(self):
        if self.pid:
            self.listener.ensure(*self._target())
            self.listener.poll()

    def read(self):
        if self.listener.why:
            return None, self.listener.why
        seen = self.listener.seen.get(self.key)
        if not seen or not seen[0]:
            return None, Waiting(f"waiting for {self.title} to report "
                                 "(it does when anything changes)")
        return seen[0], None

    def write(self, grid):
        if not ini_true(self.ini, "AcceptUDPRequests"):
            return f"turn on Accept UDP requests in {self.title} (Settings, Reporting)"
        if not ini_true(self.ini, "AutoGrid"):
            return f"turn on AutoGrid next to My Grid in {self.title} (Settings, General)"
        if self.key not in self.listener.seen or not self.listener.sock:
            return f"haven't heard from {self.title} yet"
        try:
            self.listener.send_location(self.key, grid)
        except OSError as e:
            return f"{self.title}: {e}"
        return None

    def hint(self):
        return (f"{self.title} didn't take the new grid; check Accept UDP requests "
                "and AutoGrid in its settings")


class Jtdx(Wsjtx):
    key = "jtdx"
    title = "JTDX"
    settable = False
    procs = ("jtdx",)
    ini_name = "JTDX.ini"

    def write(self, grid):
        return "JTDX can't be changed while running; set My Grid in its settings"


class Fldigi(App):
    key = "fldigi"
    title = "fldigi"
    procs = ("fldigi",)

    def read(self):
        return None, ("fldigi can't report or change its locator while running; "
                      "check it in Configure, Operator")


class Qsstv(App):
    key = "qsstv"
    title = "QSSTV"
    procs = ("qsstv",)

    def read(self):
        return None, ("QSSTV can't report or change its locator while running; "
                      "check it in Options, Configuration")


class GridTracker(App):
    key = "gridtracker"
    title = "GridTracker"
    procs = ("gridtracker2.bin", "gridtracker2", "gridtracker")
    tickable = False

    def read(self):
        return None, "follows WSJT-X / JTDX"


def default_apps(radio_addr):
    listener = WsjtxListener()
    return [Radio(radio_addr), Wsjtx(listener), Jtdx(listener), Js8Call(),
            Fldigi(), Qsstv(), GridTracker()]


# ---------------------------------------------------------------- settings

class Settings:
    """Which programs are ticked, kept in ~/.config/gps-station/apps.json."""

    def __init__(self, path=None):
        self.path = path or os.path.join(config_dir(), "gps-station", "apps.json")
        self.ticked = {}
        try:
            with open(self.path) as f:
                self.ticked = dict(json.load(f).get("ticked", {}))
        except (OSError, ValueError, AttributeError):
            pass

    def is_ticked(self, key):
        return bool(self.ticked.get(key, False))

    def set_ticked(self, key, on):
        self.ticked[key] = bool(on)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"ticked": self.ticked}, f, indent=2)
        os.replace(tmp, self.path)


# ---------------------------------------------------------------- the GPS grid

class GpsGrid:
    """The steady grid from gpsd: a new one counts after it holds GRID_STEADY_S."""

    def __init__(self, addr, chars=6, steady=gs.GRID_STEADY_S):
        self.gpsd = gs.Gpsd(addr)
        self.chars = chars
        self.steady_s = steady
        self.grid = None          # the steady grid, kept after the fix is lost
        self.candidate, self.since = None, 0.0
        self.last_fix = None

    def poll(self, timeout):
        fix = self.gpsd.poll(timeout)
        if fix:
            self.feed(fix, time.monotonic())

    def feed(self, fix, now):
        self.last_fix = now
        grid = gs.maidenhead(*fix, chars=self.chars)
        if grid != self.candidate:
            self.candidate, self.since = grid, now
        if self.candidate != self.grid and now - self.since >= self.steady_s:
            self.grid = self.candidate

    def has_fix(self, now=None):
        now = time.monotonic() if now is None else now
        return self.last_fix is not None and now - self.last_fix < 30

    def receiver(self):
        """What the receiver is doing, in words."""
        g = self.gpsd
        if g.sock is None:
            return "gpsd not reachable"
        mode = {None: "waiting for gpsd", 2: "2D fix", 3: "3D fix"}.get(g.mode, "no fix")
        if g.seen is None:
            return mode
        best = f" (best {g.best:.0f} dB-Hz)" if g.best else ""
        return f"{mode}: {g.used} used, {g.heard} heard of {g.seen} in view{best}"


# ---------------------------------------------------------------- sky map

# gpsd's gnssid numbers
SYSTEMS = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS",
           6: "GLONASS", 7: "NavIC"}


def system_of(sat):
    """The satellite system's name, from gnssid or (older gpsd) the PRN range."""
    if sat.get("gnssid") in SYSTEMS:
        return SYSTEMS[sat["gnssid"]]
    prn = sat.get("PRN") or 0
    if 1 <= prn <= 32:
        return "GPS"
    if 33 <= prn <= 64 or 120 <= prn <= 158:
        return "SBAS"
    if 65 <= prn <= 96:
        return "GLONASS"
    return "other"


def sky_position(az, el, radius):
    """(x, y) from the centre of a sky plot, north up and east right; the
    horizon is at radius, straight up at the centre."""
    r = radius * (90.0 - max(0.0, min(90.0, el))) / 90.0
    a = math.radians(az)
    return r * math.sin(a), -r * math.cos(a)


def sky_plot(sats, radius):
    """Split the satellites into those with a known direction, as
    (x, y, sat), and the PRNs of those the receiver can't place yet."""
    placed, unplaced = [], []
    for s in sats:
        el, az = s.get("el"), s.get("az")
        if el is None or az is None or el < 0:
            unplaced.append(s.get("PRN"))
        else:
            placed.append((*sky_position(az, el, radius), s))
    return placed, unplaced


# ---------------------------------------------------------------- time

NTP_GPS_FILE = os.path.join(gs.NTP_CONF_DIR, gs.NTP_FILE)


def read_ntpq():
    """`ntpq -pn` output, or None if ntpd doesn't answer."""
    try:
        r = subprocess.run(["ntpq", "-pn"], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return r.stdout if r.returncode == 0 and r.stdout.strip() else None


def ntp_status(ntpq_out, gps_file_text=""):
    """(kind, text) for what the clock is following: kind is "gps", "net",
    "none" or "down"; text says so, with the offset and the GPS's part."""
    if ntpq_out is None:
        return "down", "ntpsec isn't answering"
    rows = gs.parse_ntpq(ntpq_out)
    peer = next((r for r in rows if r[0] in "*o"), None)
    gps = next((r for r in rows if gs.is_shm(r[1])), None)
    ours = gs.read_include(gps_file_text or "")
    calibration = "" if ours is None else (
        "; GPS calibrated" if ours[2] else "; GPS not calibrated yet")
    if peer is not None and gs.is_shm(peer[1]):
        return "gps", f"locked to the GPS (offset {peer[3]:+.1f} ms){calibration}"
    if gps is None:
        role = "; the GPS isn't a time source yet"
    elif peer is not None:
        role = "; GPS standing by" + calibration
    else:
        role = calibration
    if peer is not None:
        return "net", f"synced over the Internet to {peer[1]} (offset {peer[3]:+.1f} ms){role}"
    return "none", "not synchronized" + role


def read_ntp_status():
    try:
        with open(NTP_GPS_FILE) as f:
            text = f.read()
    except OSError:
        text = ""
    return ntp_status(read_ntpq(), text)


# ---------------------------------------------------------------- the tray icon

def icon_state(mode, has_grid, time_kind, rows):
    """What the tray icon shows, and its tooltip.

    Returns (fix, time, differs, tooltip): fix is "none", "2d" or "3d";
    time is "gps", "net" or "none"; differs is True when a ticked, running
    program's grid isn't the GPS grid."""
    fix = {2: "2d", 3: "3d"}.get(mode, "none")
    time_ = time_kind if time_kind in ("gps", "net") else "none"
    off = [r["title"] for r in rows
           if r["running"] and r["ticked"] and r["tickable"] and has_grid
           and r["status"] != MATCH]
    parts = [{"none": "no fix", "2d": "2D fix", "3d": "3D fix"}[fix],
             {"gps": "time from GPS", "net": "time over the Internet",
              "none": "time not synced"}[time_],
             ("not matching: " + ", ".join(off)) if off else
             ("all programs match" if has_grid else "no grid yet")]
    return fix, time_, bool(off), " · ".join(parts)


# ---------------------------------------------------------------- tracking

class Row:
    def __init__(self, app):
        self.app = app
        self.key = app.key
        self.title = app.title
        self.tickable = app.tickable
        self.pid = None
        self.grid = None
        self.status = STOPPED
        self.why = None           # why it can't be read, or why it can't be set
        self.writable = False
        self.matched = False      # has matched the GPS since it started / the GPS moved
        self.sent_at = None       # when we last sent it a grid, while waiting to see it
        self.failed = None        # why the last update failed, until it is retried
        self.notified = False

    def snapshot(self, ticked):
        return {"key": self.key, "title": self.title, "tickable": self.tickable,
                "ticked": ticked, "running": self.pid is not None, "grid": self.grid,
                "status": self.status, "why": self.why, "writable": self.writable}


class Tracker:
    def __init__(self, apps, settings, notify=None, clock=time.monotonic):
        self.rows = [Row(a) for a in apps]
        self.settings = settings
        self.notify = notify or (lambda title, text: None)
        self.clock = clock
        self.gps = None

    def row(self, key):
        return next(r for r in self.rows if r.key == key)

    def update(self, gps_grid, procs):
        now = self.clock()
        if gps_grid != self.gps:
            for r in self.rows:           # a new square: earlier hand-set grids no longer count
                r.matched, r.failed, r.sent_at = False, None, None
            self.gps = gps_grid
        for r in self.rows:
            self._update_row(r, procs, now)

    def _update_row(self, r, procs, now):
        pid = r.app.present(procs)
        if pid != r.pid:                  # started, stopped or restarted
            r.pid, r.matched, r.failed, r.sent_at, r.notified = pid, False, None, None, False
        if pid is None:
            r.status, r.grid, r.why, r.writable = STOPPED, None, None, False
            return
        r.app.tick()
        if hasattr(r.app, "label"):
            r.title = r.app.label()
        grid, why = r.app.read()
        r.grid = grid
        if grid is None:
            r.status, r.why = UNKNOWN, why
        elif self.gps is None:
            r.status, r.why = NOGPS, None
        elif grids_match(grid, self.gps):
            r.status, r.why = MATCH, None
            r.matched, r.failed, r.sent_at = True, None, None
        else:
            r.status = MANUAL if r.matched else STALE
            r.why = None
        r.writable = r.app.settable and self.gps is not None
        if not r.tickable or self.gps is None:
            return
        ticked = self.settings.is_ticked(r.key)
        if r.sent_at is not None and r.status != MATCH and now - r.sent_at >= VERIFY_S:
            r.sent_at, r.failed = None, r.app.hint()
            if ticked:
                self._tell(r, r.failed)
        if r.failed and r.status != MATCH:
            r.why = r.failed
        if not ticked:
            return
        if r.status == UNKNOWN:
            if not isinstance(r.why, Waiting):
                self._tell(r, r.why)
        elif r.status == STALE and r.sent_at is None and not r.failed:
            self._send(r, now)

    def _send(self, r, now):
        why = r.app.write(self.gps)
        if why:
            r.failed = r.why = why
            self._tell(r, why)
        else:
            log(f"apps: sent {self.gps} to {r.title}")
            r.sent_at = now

    def _tell(self, r, why):
        if not r.notified:
            r.notified = True
            have = f" has {r.grid}" if r.grid else ""
            self.notify(f"{r.title}{have}; GPS grid is {self.gps}", why)

    def force(self, key):
        r = self.row(key)
        if r.pid is None or self.gps is None:
            return
        r.failed, r.notified = None, False
        self._send(r, self.clock())

    def snapshot(self):
        return [r.snapshot(self.settings.is_ticked(r.key)) for r in self.rows]
