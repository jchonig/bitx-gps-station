#!/usr/bin/env python3
"""gps_station_desktop.py - tray icon that keeps running ham programs on the GPS grid.

Runs in the desktop session (started from /etc/xdg/autostart). Clicking the
tray icon opens a window with a row per supported program it finds:

  [x] WSJT-X     ✗ AA11AA  stale              [Update]

The box says whether gps-station may change that program's grid. A ticked
program with an out-of-date grid is updated; one whose grid was set by hand
is left alone until the GPS moves to another square or Update is pressed.
When a ticked program can't be updated, a desktop notification says why,
once per launch of that program.

The radio itself is set by the gps-station system service; its row here
shows its state, and Update sets it at once.

    gps-station-desktop                 # normally started at login
    gps-station-desktop --show          # open the window at start
"""
import argparse
import queue
import shutil
import subprocess
import threading
import time

import gi

gi.require_version("Gtk", "3.0")
from gi.repository import GLib, Gtk  # noqa: E402

import gps_station as gs  # noqa: E402
import gps_station_apps as apps  # noqa: E402

try:
    gi.require_version("Notify", "0.7")
    from gi.repository import Notify  # noqa: E402
except (ValueError, ImportError):
    Notify = None

CHECK_S = 10              # between looks at the running programs

ICONS = {"ok": "gps-station-ok", "differs": "gps-station-differs", "nofix": "gps-station-nofix"}

MARK = {
    apps.MATCH: "✓",
    apps.STALE: "✗",
    apps.MANUAL: "✋",
    apps.UNKNOWN: "?",
    apps.STOPPED: "",
    apps.NOGPS: "",
}

WORD = {
    apps.MATCH: "",
    apps.STALE: "stale",
    apps.MANUAL: "set by hand",
    apps.UNKNOWN: "can't read",
    apps.STOPPED: "not running",
    apps.NOGPS: "",
}


class Notifier:
    """Desktop notifications, falling back to zenity, then to the log."""

    def __init__(self):
        self.ok = bool(Notify) and Notify.init("gps-station")

    def __call__(self, title, text):
        gs.log(f"notify: {title}: {text}")
        if self.ok:
            try:
                Notify.Notification.new(title, text, ICONS["differs"]).show()
                return
            except GLib.Error:
                pass
        if shutil.which("zenity"):
            subprocess.Popen(["zenity", "--notification", f"--text={title}\n{text}"])


class Worker(threading.Thread):
    """Reads gpsd and checks the programs, away from the GTK main loop."""

    def __init__(self, tracker, gps, on_update):
        super().__init__(daemon=True)
        self.tracker = tracker
        self.gps = gps
        self.on_update = on_update
        self.commands = queue.Queue()

    def run(self):
        next_check = 0.0
        while True:
            self.gps.poll(0.5)
            forced = []
            while not self.commands.empty():
                forced.append(self.commands.get())
            now = time.monotonic()
            if now < next_check and not forced:
                continue
            next_check = now + CHECK_S
            self.tracker.update(self.gps.grid, apps.running())
            for key in forced:
                self.tracker.force(key)
            if forced:
                self.tracker.update(self.gps.grid, apps.running())
            state = {"grid": self.gps.grid, "candidate": self.gps.candidate,
                     "fix": self.gps.has_fix(), "receiver": self.gps.receiver(),
                     "rows": self.tracker.snapshot()}
            GLib.idle_add(self.on_update, state)


class Window(Gtk.Window):
    def __init__(self, settings, worker):
        super().__init__(title="GPS grid")
        self.settings = settings
        self.worker = worker
        self.set_border_width(12)
        self.set_icon_name(ICONS["nofix"])
        self.set_skip_taskbar_hint(True)      # it lives in the tray
        self.set_skip_pager_hint(True)
        self.set_keep_above(True)             # the radio's own window is full-screen
        self.set_position(Gtk.WindowPosition.CENTER)
        self.connect("delete-event", lambda *a: self.hide() or True)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        self.gps_label = Gtk.Label(xalign=0)
        box.pack_start(self.gps_label, False, False, 0)
        self.receiver_label = Gtk.Label(xalign=0)
        box.pack_start(self.receiver_label, False, False, 0)
        self.grid = Gtk.Grid(column_spacing=12, row_spacing=8)
        box.pack_start(self.grid, True, True, 0)
        self.empty = Gtk.Label(label="No supported programs are running.", xalign=0)
        box.pack_start(self.empty, False, False, 0)
        self.set_default_size(560, -1)
        self.add(box)
        self.widgets = {}

    def refresh(self, state):
        if state["grid"] is None:
            self.gps_label.set_markup("<b>GPS:</b> no fix yet")
        else:
            extra = "" if state["fix"] else "  (fix lost; last grid)"
            if state["candidate"] and state["candidate"] != state["grid"] and state["fix"]:
                extra = f"  (moving to {state['candidate']})"
            self.gps_label.set_markup(f"<b>GPS:</b> {state['grid']}{extra}")
        self.receiver_label.set_markup(
            f"<small>{GLib.markup_escape_text(state['receiver'])}</small>")
        for row in state["rows"]:
            if not row["running"] and row["key"] not in self.widgets:
                continue
            w = self.widgets.get(row["key"]) or self._add_row(row, len(self.widgets))
            self._fill(w, row)
        self.empty.set_visible(not any(r["running"] for r in state["rows"]))

    def _add_row(self, row, n):
        check = Gtk.CheckButton()
        check.set_active(row["ticked"])
        check.set_sensitive(row["tickable"])
        check.connect("toggled",
                      lambda b, k=row["key"]: self.settings.set_ticked(k, b.get_active()))
        name = Gtk.Label(xalign=0)
        status = Gtk.Label(xalign=0, hexpand=True)
        status.set_line_wrap(True)
        status.set_width_chars(30)
        status.set_max_width_chars(50)
        button = Gtk.Button(label="Update", valign=Gtk.Align.CENTER)
        check.set_valign(Gtk.Align.CENTER)
        button.connect("clicked", lambda b, k=row["key"]: self.worker.commands.put(k))
        for col, w in enumerate((check, name, status, button)):
            self.grid.attach(w, col, n, 1, 1)
        self.grid.show_all()
        w = {"check": check, "name": name, "status": status, "button": button}
        self.widgets[row["key"]] = w
        return w

    def _fill(self, w, row):
        w["name"].set_text(row["title"])
        mark, word = MARK[row["status"]], WORD[row["status"]]
        text = " ".join(x for x in (mark, row["grid"] or "", word) if x)
        if row["why"] and row["status"] != apps.STOPPED:
            text += f"\n<small>{GLib.markup_escape_text(row['why'])}</small>"
        w["status"].set_markup(text if "<small>" in text else GLib.markup_escape_text(text))
        settable = row["writable"] and row["running"] and row["status"] != apps.MATCH
        w["button"].set_sensitive(settable)
        w["button"].set_visible(row["tickable"])
        tip = None if row["writable"] else (row["why"] or "can't be changed while running")
        w["button"].set_tooltip_text(tip)


def overall(state):
    """(icon key, tooltip) for the tray icon and the window."""
    rows = [r for r in state["rows"] if r["running"] and r["ticked"]]
    if state["grid"] is None:
        return "nofix", f"GPS grid: no fix yet ({state['receiver']})"
    off = [r["title"] for r in rows if r["status"] != apps.MATCH]
    if off:
        return "differs", f"GPS grid {state['grid']}; not matching: {', '.join(off)}"
    return "ok", f"GPS grid {state['grid']}"


class Tray:
    def __init__(self, window):
        self.window = window
        self.icon = Gtk.StatusIcon.new_from_icon_name(ICONS["nofix"])
        self.icon.set_title("GPS grid")
        self.icon.set_tooltip_text("GPS grid: no fix yet")
        self.icon.connect("activate", lambda *a: self.toggle())
        self.icon.connect("popup-menu", self.menu)

    def toggle(self):
        if self.window.get_visible():
            self.window.hide()
        else:
            self.window.show_all()
            self.window.present_with_time(Gtk.get_current_event_time())

    def menu(self, icon, button, when):
        m = Gtk.Menu()
        show = Gtk.MenuItem(label="Show")
        show.connect("activate", lambda *a: self.toggle())
        quit_ = Gtk.MenuItem(label="Quit")
        quit_.connect("activate", lambda *a: Gtk.main_quit())
        m.append(show)
        m.append(quit_)
        m.show_all()
        m.popup(None, None, Gtk.StatusIcon.position_menu, icon, button, when)

    def refresh(self, state):
        name, tip = overall(state)
        self.icon.set_from_icon_name(ICONS[name])
        self.icon.set_tooltip_text(tip)
        self.window.set_icon_name(ICONS[name])


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--radio", default="127.0.0.1:8081", help="host:port of the radio")
    ap.add_argument("--gpsd", default="127.0.0.1:2947", help="host:port of gpsd")
    ap.add_argument("--chars", type=int, choices=(4, 6), default=6, help="grid length")
    ap.add_argument("--show", action="store_true", help="open the window at start")
    a = ap.parse_args()

    settings = apps.Settings()
    tracker = apps.Tracker(apps.default_apps(gs.hostport(a.radio, 8081)), settings,
                           notify=lambda t, x: GLib.idle_add(notifier, t, x))
    notifier = Notifier()
    gps = apps.GpsGrid(gs.hostport(a.gpsd, 2947), chars=a.chars)

    window = tray = None

    def on_update(state):
        window.refresh(state)
        tray.refresh(state)
        return False

    worker = Worker(tracker, gps, on_update)
    window = Window(settings, worker)
    tray = Tray(window)
    if a.show:
        tray.toggle()
    worker.start()
    Gtk.main()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
