#!/usr/bin/env python3
"""gps_station.py - feed a GPS fix to the radio and to ntpsec.

Runs beside gpsd on an sBitx or zBitx and does two jobs:

TIME. When gpsd starts writing time to shared memory (that is, the receiver
has a fix), it writes the GPS refclock to /etc/ntpsec/conf.d/gps-station.conf,
adds "includefile /etc/ntpsec/conf.d/" (every *.conf there) to the end of
/etc/ntpsec/ntp.conf (once), and restarts ntpsec. Later, whenever ntpd is
synced to a network server and has heard the GPS for a while, it measures
how late the GPS's NMEA time arrives, sets that as time1, restarts ntpsec
once more, and leaves it alone from then on. With the network up, the
internet servers outvote an uncalibrated GPS; in the field, the GPS is
what keeps the clock, which is why the file also sets "tos minsane 1".

GRID. It works out the Maidenhead locator from the fix and sets it on the
radio over TCP port 8081, retrying until the radio program answers and the
new value reads back. It speaks both programs found on that port:

  * zbitxre:              "MYGRID FN12oj"  (protocol in zbitxre's PROTOCOL.md)
  * sbitx (JJ's, W9JES):  "grid FN12oj", read back with "?MYGRID"

The grid is sent when it changes, after it has held steady for a minute,
so a fix wandering on a grid line doesn't flip it back and forth, and a
grid you type on the radio yourself is left alone until you move.

Python 3 standard library only. Run as root for the ntpsec part (it reads
gpsd's root-only shared memory and edits the config); the grid part alone
needs no privileges.

    sudo python3 gps_station.py             # both jobs
    python3 gps_station.py --no-ntp         # grid only
    sudo python3 gps_station.py --dry-run   # say what it would do
"""
import argparse
import ctypes
import json
import os
import re
import select
import socket
import statistics
import subprocess
import time

__version__ = "dev"       # the package build puts the release number here
HOMEPAGE = "https://github.com/jchonig/bitx-gps-station"

GPSD = ("127.0.0.1", 2947)
RADIO = ("127.0.0.1", 8081)
NTP_CONF = "/etc/ntpsec/ntp.conf"
NTP_CONF_DIR = "/etc/ntpsec/conf.d"     # included from ntp.conf: every *.conf in it
NTP_FILE = "gps-station.conf"           # ours, in NTP_CONF_DIR
NTP_SERVICE = "ntpsec"

GRID_STEADY_S = 60        # a new grid must hold this long before it is sent
RADIO_RETRY_S = 10        # between attempts to reach the radio
SHM_CHECK_S = 5           # between looks at gpsd's shared memory
CAL_EVERY_S = 64          # between offset samples (one ntpd poll)
CAL_SAMPLES = 10          # samples whose median sets time1
CAL_SKIP_MS = 2.0         # an offset smaller than this needs no change

BEGIN = "# gps_station: begin"
END = "# gps_station: end"


def log(msg):
    print(time.strftime("%H:%M:%S "), msg, sep="", flush=True)


# ---------------------------------------------------------------- grid

def maidenhead(lat, lon, chars=6):
    lon += 180.0
    lat += 90.0
    g = chr(ord("A") + int(lon // 20)) + chr(ord("A") + int(lat // 10))
    g += str(int((lon % 20) // 2)) + str(int(lat % 10))
    g += chr(ord("a") + int((lon % 2) * 12)) + chr(ord("a") + int((lat % 1) * 24))
    return g[:chars]


# ---------------------------------------------------------------- gpsd

class Gpsd:
    """A watching client of gpsd that hands back fixes as they come.

    Also keeps the receiver's state for display: mode (0/1 no fix, 2 = 2D,
    3 = 3D); satellites in view, heard (any signal) and used; and the best
    signal in dB-Hz. Each is None until gpsd says.

    sats is the latest satellite list from SKY: dicts with PRN, gnssid,
    az, el, ss and used, as gpsd sends them.

    shm_unit is the NTP shared-memory unit gpsd writes time to, from its
    TOFF messages: -1 means gpsd has none (it wasn't started with -n)."""

    def __init__(self, addr):
        self.addr = addr
        self.sock = None
        self.buf = b""
        self.next_try = 0.0
        self.mode = None
        self.seen = self.heard = self.used = self.best = None
        self.sats = []
        self.shm_unit = None

    def _connect(self):
        if time.monotonic() < self.next_try:
            return False
        try:
            self.sock = socket.create_connection(self.addr, timeout=3)
            self.sock.sendall(b'?WATCH={"enable":true,"json":true,"pps":true}\n')
            self.sock.setblocking(False)
            self.buf = b""
            log("gpsd: connected")
            return True
        except OSError as e:
            log(f"gpsd: {e}; retrying")
            self.sock = None
            self.next_try = time.monotonic() + RADIO_RETRY_S
            return False

    def poll(self, timeout):
        """Wait up to timeout seconds; return the latest (lat, lon) fix, or None."""
        if self.sock is None and not self._connect():
            time.sleep(timeout)
            return None
        fix = None
        ready, _, _ = select.select([self.sock], [], [], timeout)
        if not ready:
            return None
        try:
            data = self.sock.recv(65536)
        except BlockingIOError:
            return None
        except OSError:
            data = b""
        if not data:
            log("gpsd: connection lost")
            self.sock.close()
            self.sock = None
            self.mode = self.seen = self.heard = self.used = self.best = None
            self.sats = []
            self.shm_unit = None
            self.next_try = time.monotonic() + RADIO_RETRY_S
            return None
        self.buf += data
        *lines, self.buf = self.buf.split(b"\n")
        for line in lines:
            try:
                msg = json.loads(line)
            except ValueError:
                continue
            if msg.get("class") == "TPV":
                self.mode = msg.get("mode", 0)
                if self.mode >= 2 and "lat" in msg and "lon" in msg:
                    fix = (msg["lat"], msg["lon"])
            elif msg.get("class") == "SKY":
                self._sky(msg)
            elif msg.get("class") == "TOFF":
                m = re.fullmatch(r"NTP(-?\d+)", str(msg.get("shm", "")))
                if m:
                    self.shm_unit = int(m.group(1))
        return fix

    def _sky(self, msg):
        sats = msg.get("satellites")
        if sats is None:
            return                        # a partial SKY: keep what we had
        self.sats = sats
        self.seen = msg.get("nSat", len(sats))
        self.used = msg.get("uSat", sum(1 for s in sats if s.get("used")))
        signals = [s.get("ss") or 0 for s in sats]
        self.heard = sum(1 for ss in signals if ss > 0)
        self.best = max(signals, default=0)


# ---------------------------------------------------------------- radio

def _read_for(sock, seconds):
    sock.settimeout(0.2)
    end = time.monotonic() + seconds
    out = b""
    while time.monotonic() < end:
        try:
            chunk = sock.recv(65536)
        except socket.timeout:
            continue
        if not chunk:
            break
        out += chunk
    return out.decode("utf-8", "replace")


def _session(addr, send=None, listen=1.5):
    """Connect, collect what the radio says first, optionally send a line and
    collect the answer. Returns (greeting, answer)."""
    with socket.create_connection(addr, timeout=3) as s:
        greeting = _read_for(s, listen)
        answer = ""
        if send is not None:
            s.sendall(send.encode() + b"\n")
            answer = _read_for(s, 0.8)
        return greeting, answer


def _zbitxre_grid(greeting):
    for line in greeting.splitlines():
        name, _, value = line.partition(" ")
        if name == "MYGRID":
            return value.strip()
    return None


def which_radio(greeting):
    if "\x1b[" in greeting or re.search(r"sbitx v\d", greeting, re.I):
        return "sbitx"
    if re.search(r"^(FREQ|MODE|MYGRID) ", greeting, re.M):
        return "zbitxre"
    return None


def set_radio_grid(addr, grid):
    """Set the grid and read it back. Returns True once the radio has it."""
    greeting, _ = _session(addr)
    kind = which_radio(greeting)
    if kind == "zbitxre":
        if _zbitxre_grid(greeting) == grid:
            return True
        _, answer = _session(addr, f"MYGRID {grid}")
        if "ERROR" in answer:
            log(f"radio: zbitxre refused it: {answer.strip()}")
            return False
        return _zbitxre_grid(_session(addr)[0]) == grid
    if kind == "sbitx":
        # one command per connection: the sbitx reads a single line per recv
        _session(addr, f"grid {grid}")
        _, answer = _session(addr, "?MYGRID")
        return grid.lower() in answer.lower()
    log("radio: something answered on the port but it isn't a radio I know")
    return False


# ---------------------------------------------------------------- gpsd's shared memory

class ShmWatch:
    """Tells which NTP shared-memory unit gpsd is writing, by its count moving."""

    KEY = 0x4E545030          # "NTP0"
    SHM_RDONLY = 0o10000

    def __init__(self):
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.libc.shmget.argtypes = [ctypes.c_int, ctypes.c_size_t, ctypes.c_int]
        self.libc.shmat.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_int]
        self.libc.shmat.restype = ctypes.c_void_p
        self.libc.shmdt.argtypes = [ctypes.c_void_p]
        self.last = {}

    def _count(self, unit):
        shmid = self.libc.shmget(self.KEY + unit, 0, 0)
        if shmid < 0:
            return None
        addr = self.libc.shmat(shmid, None, self.SHM_RDONLY)
        if addr in (None, ctypes.c_void_p(-1).value):
            return None
        try:
            return ctypes.c_int.from_address(addr + 4).value   # struct shmTime.count
        finally:
            self.libc.shmdt(addr)

    def active_unit(self):
        """The even unit (time from NMEA) whose count has moved since last asked."""
        found = None
        for unit in (0, 2, 4, 6):
            n = self._count(unit)
            if n is not None and unit in self.last and n != self.last[unit] and found is None:
                found = unit
            self.last[unit] = n
        return found


# ---------------------------------------------------------------- ntpsec

def read_include(text):
    """Our include file's (unit, time1, calibrated), or None if it has no refclock."""
    m = re.search(r"calibrated=(\w+).*?^refclock shm unit (\d+) refid GPS time1 (-?[\d.]+)",
                  text, re.S | re.M)
    if not m:
        return None
    return int(m.group(2)), float(m.group(3)), m.group(1) == "yes"


def include_text(unit, time1, calibrated):
    return (f"# Written by gps-station; changes are overwritten. "
            f"calibrated={'yes' if calibrated else 'no'}\n"
            "# Let the GPS alone set the clock when no network server is reachable\n"
            "# (Debian's ntp.conf asks for 3 agreeing sources).\n"
            "tos minsane 1\n"
            f"refclock shm unit {unit} refid GPS time1 {time1:.4f}\n")


def include_line(conf_dir):
    return f"includefile {conf_dir.rstrip('/')}/"


def has_include(conf_text, conf_dir):
    d = re.escape(conf_dir.rstrip("/"))
    return re.search(r"^\s*includefile\s+" + d + r"/?\s*$", conf_text, re.M) is not None


def add_include(conf_text, conf_dir):
    """ntp.conf with a line including every *.conf in conf_dir, at the end
    (so a tos there wins), and without the block older versions wrote into
    ntp.conf itself."""
    text = strip_old_block(conf_text)
    if has_include(text, conf_dir):
        return text
    if not text.endswith("\n"):
        text += "\n"
    return (text + f"# Added by gps-station: also read every *.conf in {conf_dir}\n"
            + include_line(conf_dir) + "\n")


# Older versions kept the refclock in a block in ntp.conf itself.
BEGIN = "# gps_station: begin"
END = "# gps_station: end"


def read_old_block(conf_text):
    """The old block's (unit, time1, calibrated), or None if it isn't there."""
    m = re.search(re.escape(BEGIN) + r".*?calibrated=(\w+).*?\n"
                  r"refclock shm unit (\d+) refid GPS time1 (-?[\d.]+)",
                  conf_text, re.S)
    if not m:
        return None
    return int(m.group(2)), float(m.group(3)), m.group(1) == "yes"


def strip_old_block(conf_text):
    return re.sub(re.escape(BEGIN) + r".*?" + re.escape(END) + r"\n?", "",
                  conf_text, flags=re.S)


def foreign_shm(conf_text):
    """A refclock shm line (in ntp.conf or another included file) that we didn't write."""
    return re.search(r"^\s*refclock\s+shm\b", strip_old_block(conf_text), re.M) is not None


def other_conf_files(conf_dir, ours):
    """The *.conf files in conf_dir besides ours: they are read by ntpd too."""
    try:
        names = sorted(os.listdir(conf_dir))
    except OSError:
        return []
    return [os.path.join(conf_dir, n) for n in names
            if n.endswith(".conf") and os.path.join(conf_dir, n) != ours]


def parse_ntpq(text):
    """Rows of `ntpq -pn`: (tally, remote, reach, offset_ms)."""
    rows = []
    for line in text.splitlines():
        if not line or line.startswith(("=", "     remote")):
            continue
        tally, fields = line[0], line[1:].split()
        if len(fields) < 10:
            continue
        try:
            rows.append((tally, fields[0], fields[-4], float(fields[-2])))
        except ValueError:
            continue
    return rows


def is_shm(remote):
    return remote.startswith("SHM(") or remote.startswith("127.127.28.")


class Ntp:
    """Keeps the GPS refclock in conf.d/gps-station.conf; ntp.conf gets one
    line including the conf.d directory."""

    def __init__(self, conf, service, dry_run, conf_dir=NTP_CONF_DIR):
        self.conf = conf
        self.conf_dir = conf_dir
        self.include = os.path.join(conf_dir, NTP_FILE)
        self.service = service
        self.dry_run = dry_run
        self.shm = ShmWatch()
        self.next_shm = 0.0
        self.next_cal = 0.0
        self.samples = []
        self.gave_up = False
        self.warned_no_shm = False
        self.virtual = {}         # path -> text, as a dry run would have left it

    def _read(self, path):
        if path in self.virtual:
            return self.virtual[path]
        try:
            with open(path) as f:
                return f.read()
        except FileNotFoundError:
            if path == self.include:
                return ""
            raise

    def _write(self, path, text):
        if self.dry_run:
            self.virtual[path] = text
            log(f"ntp: would write {path}:\n{text}")
            return
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".gps_station.tmp"
        with open(tmp, "w") as f:
            f.write(text)
        try:
            os.chmod(tmp, os.stat(path).st_mode)
        except FileNotFoundError:
            os.chmod(tmp, 0o644)
        os.rename(tmp, path)

    def _restart(self, why):
        if self.dry_run:
            log(f"ntp: {why}; would restart {self.service}")
            return
        log(f"ntp: {why}; restarting {self.service}")
        subprocess.run(["systemctl", "restart", self.service], check=False)

    def _save(self, conf, unit, time1, calibrated, why):
        """Write our file, make sure ntp.conf includes it, restart ntpsec."""
        self._write(self.include, include_text(unit, time1, calibrated))
        wanted = add_include(conf, self.conf_dir)
        if wanted != conf:
            self._write(self.conf, wanted)
        self._restart(why)

    def tick(self, shm_unit=None):
        """shm_unit: the unit gpsd says it writes time to (Gpsd.shm_unit)."""
        if self.gave_up:
            return
        if shm_unit == -1 and not self.warned_no_shm:
            log("ntp: gpsd has a fix but isn't writing time to shared memory; "
                'add -n to GPSD_OPTIONS in /etc/default/gpsd and restart gpsd')
            self.warned_no_shm = True
        elif shm_unit is not None and shm_unit >= 0:
            self.warned_no_shm = False
        now = time.monotonic()
        try:
            conf = self._read(self.conf)
            ours = read_include(self._read(self.include))
            others = {p: self._read(p) for p in other_conf_files(self.conf_dir, self.include)}
        except OSError as e:
            log(f"ntp: can't read ntpsec's config: {e}; leaving ntpsec alone")
            self.gave_up = True
            return
        for path, text in [(self.conf, conf), *others.items()]:
            if foreign_shm(text):
                log(f"ntp: {path} already has a refclock shm line I didn't write; "
                    "leaving ntpsec alone")
                self.gave_up = True
                return
        old = read_old_block(conf)
        if old and not ours:
            self._save(conf, *old, f"moved the GPS refclock from {self.conf} to {self.include}")
            return
        if ours and not has_include(conf, self.conf_dir):
            self._save(conf, *ours, f"{self.conf} had lost its includefile line")
            return

        if now >= self.next_shm:
            self.next_shm = now + SHM_CHECK_S
            unit = self.shm.active_unit()
            if unit is not None and (ours is None or ours[0] != unit):
                time1 = ours[1] if ours else 0.0
                self._save(conf, unit, time1, False,
                           f"GPS has a fix, adding refclock shm unit {unit}")
                self.samples = []
                self.next_cal = now + 15 * 60     # let ntpd settle first
                return

        if ours is None or ours[2] or now < self.next_cal:
            return
        self.next_cal = now + CAL_EVERY_S
        try:
            out = subprocess.run(["ntpq", "-pn"], capture_output=True, text=True,
                                 timeout=10).stdout
        except (OSError, subprocess.TimeoutExpired):
            return
        rows = parse_ntpq(out)
        gps = [r for r in rows if is_shm(r[1])]
        net_synced = any(r[0] == "*" and not is_shm(r[1]) for r in rows)
        if not gps or gps[0][2] != "377" or not net_synced:
            self.samples = []
            return
        self.samples.append(gps[0][3])
        if len(self.samples) < CAL_SAMPLES:
            return
        offset = statistics.median(self.samples)
        self.samples = []
        unit, time1, _ = ours
        if abs(offset) < CAL_SKIP_MS:
            new = time1
        else:
            new = time1 - offset / 1000.0     # GPS reads late -> negative offset -> add delay
        self._save(conf, unit, new, True,
                   f"GPS offset {offset:+.1f} ms against the network; "
                   f"time1 {time1:.4f} -> {new:.4f}")


# ---------------------------------------------------------------- main

def hostport(s, default_port):
    host, _, port = s.rpartition(":")
    return (host or s, int(port)) if port.isdigit() else (s, default_port)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--radio", default="127.0.0.1:8081", help="host:port of the radio")
    ap.add_argument("--gpsd", default="127.0.0.1:2947", help="host:port of gpsd")
    ap.add_argument("--chars", type=int, choices=(4, 6), default=6, help="grid length")
    ap.add_argument("--conf", default=NTP_CONF, help="ntpsec's main config")
    ap.add_argument("--conf-dir", default=NTP_CONF_DIR,
                    help="directory included from --conf; the refclock goes in "
                         f"{NTP_FILE} there")
    ap.add_argument("--service", default=NTP_SERVICE)
    ap.add_argument("--no-ntp", action="store_true", help="only set the grid")
    ap.add_argument("--no-grid", action="store_true", help="only look after ntpsec")
    ap.add_argument("--dry-run", action="store_true", help="change nothing, say what it would do")
    a = ap.parse_args()

    radio = hostport(a.radio, 8081)
    gpsd = Gpsd(hostport(a.gpsd, 2947))
    ntp = None
    if not a.no_ntp:
        if os.geteuid() != 0 and not a.dry_run:
            log("ntp: not root, so ntpsec is left alone (use sudo, or --no-ntp to hush this)")
        else:
            ntp = Ntp(a.conf, a.service, a.dry_run, conf_dir=a.conf_dir)

    candidate, since = None, 0.0
    sent, pending, next_try = None, None, 0.0
    while True:
        fix = gpsd.poll(1.0)
        now = time.monotonic()
        if fix and not a.no_grid:
            grid = maidenhead(*fix, chars=a.chars)
            if grid != candidate:
                candidate, since = grid, now
            elif grid != sent and grid != pending and now - since >= GRID_STEADY_S:
                pending, next_try = grid, now
                log(f"grid: {grid}")
        if pending and now >= next_try:
            try:
                ok = a.dry_run or set_radio_grid(radio, pending)
            except OSError as e:
                ok = False
                log(f"radio: {e}; retrying in {RADIO_RETRY_S} s")
            if ok:
                log(f"radio: MYGRID is {pending}" + (" (dry run)" if a.dry_run else ""))
                sent, pending = pending, None
            else:
                next_try = now + RADIO_RETRY_S
        if ntp:
            ntp.tick(gpsd.shm_unit)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
