# Installing and configuring gps-station

See the [README](../README.md) for what gps-station does.

## Requirements

- An sBitx or zBitx on Raspberry Pi OS (Bullseye or later; 32- or 64-bit)
- A GPS receiver that gpsd can read; see
  [GPS receivers](../README.md#gps-receivers) for which are picked up
  automatically, and [Configuring gpsd](#configuring-gpsd) for the rest
- `gpsd`, `python3` (3.9 or later), and `ntpsec` for the time job
- For the time job, gpsd started with `-n`, so it feeds time to shared
  memory (see [Configuring gpsd](#configuring-gpsd))

The package is architecture `all`: the same `.deb` installs on 32-bit
(armhf) and 64-bit (arm64) systems.

## Install

### From the apt repository

```sh
sudo curl -fsSL -o /usr/share/keyrings/gps-station.gpg \
    https://gps-station.n2vlv.net/gps-station.gpg
echo "deb [signed-by=/usr/share/keyrings/gps-station.gpg] \
https://gps-station.n2vlv.net stable main" |
    sudo tee /etc/apt/sources.list.d/gps-station.list
sudo apt update
sudo apt install gps-station
```

Later releases arrive with `sudo apt upgrade`.

### From a release download

Download `gps-station_<version>_all.deb` from the
[Releases page](https://github.com/jchonig/bitx-gps-station/releases),
then:

```sh
sudo apt install ./gps-station_*_all.deb
```

Either way, the service is enabled and started on install, and the tray
icon starts at your next desktop login (or run `gps-station-desktop`).
The tray icon needs `python3-gi` and GTK 3, which the package recommends
and Raspberry Pi OS desktop images already have. Watch the service with:

```sh
journalctl -u gps-station -f
```

### By hand, without a package

From a clone of this repository, for the service alone (no tray icon,
and no boot-time multicast for `lo`):

```sh
sudo install -D -m 755 src/gps_station.py /usr/lib/gps-station/gps_station.py
sudo ln -s ../lib/gps-station/gps_station.py /usr/sbin/gps-station
sudo install -m 644 debian/gps-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gps-station
```

## Configuring gpsd

gpsd's settings are in `/etc/default/gpsd`; restart it after changing
them with `sudo systemctl restart gpsd`. The package doesn't change them
itself.

**`-n` is needed for the time job.** Without it, gpsd hands fixes to
programs that ask but never writes time to the shared memory ntpsec
reads, and the GPS never becomes a time source:

```sh
GPSD_OPTIONS="-n"
```

If it is missing, the service says so in its log once it has a fix
(`journalctl -u gps-station`).

**A receiver gpsd doesn't pick up by itself** (see
[GPS receivers](../README.md#gps-receivers)) has to be named in
`DEVICES`, preferably by its stable `/dev/serial/by-id/…` name. For
example, for a receiver on a Prolific PL2303 chip:

```sh
DEVICES="/dev/serial/by-id/usb-Prolific_Technology_Inc._USB-Serial_Controller-if00-port0"
```

`ls /dev/serial/by-id/` lists the names of what's plugged in; a module
on the Pi's serial pins is usually `/dev/serial0`.

## Configuring the ham programs

The tray app can change a running program only if that program allows
it. The settings to turn on, per program:

- **WSJT-X**: Settings, Reporting: Accept UDP requests; and Settings,
  General: AutoGrid, next to My Grid.
- **JS8Call**: Settings, Reporting: turn on the TCP server API and Accept
  TCP requests.
- **GridTracker**: follows WSJT-X; to share WSJT-X's messages with it, see
  below.
- **JTDX, fldigi, QSSTV**: nothing to turn on; they can't be changed while
  running, so set the grid in them by hand (JTDX: My Grid; fldigi:
  Configure, Operator).
- **The radio**: nothing; the service sets it.

### Sharing WSJT-X with GridTracker

WSJT-X and JTDX send their UDP messages to the address set in Settings,
Reporting, UDP Server, and the tray app listens there. GridTracker
normally listens on that address too, and only one program can listen
on a plain address such as `127.0.0.1:2237`; the WSJT-X row then says the
port is taken. To share it, use a multicast address, which any number of
programs can hear:

1. WSJT-X (and JTDX): Settings, Reporting: UDP Server `239.255.0.1`,
   port `2237`, Outgoing interfaces `lo`.
2. GridTracker2: turn on Multicast and give it the same address.

Linux leaves multicast off on the loopback interface, so WSJT-X doesn't
offer `lo` until it is on. The package's `gps-station-lo-multicast`
service turns it on at boot (`ip link set dev lo multicast on`); using
`lo` keeps the stream on the radio and working without Wi-Fi. Restart
WSJT-X after the first install so it sees the change. The tray app reads
the address from WSJT-X's settings and needs no setting of its own.

## Recalibrating the GPS time

The service measures the GPS's delay once (see
[Calibrating the GPS time](../README.md#calibrating-the-gps-time)). To
measure again, for example after changing the receiver or its settings,
change `calibrated=yes` to `calibrated=no` in
`/etc/ntpsec/conf.d/gps-station.conf` and restart the service:

```sh
sudo systemctl restart gps-station
```

## Configuring gps-station

`man gps-station` and `man gps-station-desktop` describe both commands.

Options go in `GPS_STATION_ARGS` in `/etc/default/gps-station`; restart
the service after changing it.

| Option              | Effect                                    |
| ------------------- | ----------------------------------------- |
| `--chars 4`         | send a 4-character grid instead of 6      |
| `--no-ntp`          | only set the grid; leave ntpsec alone     |
| `--no-grid`         | only look after ntpsec                    |
| `--radio HOST:PORT` | radio software's port (`127.0.0.1:8081`)  |
| `--gpsd HOST:PORT`  | gpsd's port (`127.0.0.1:2947`)            |
| `--conf PATH`       | ntpsec config (`/etc/ntpsec/ntp.conf`)    |
| `--conf-dir PATH`   | included directory (`/etc/ntpsec/conf.d`) |
| `--service NAME`    | ntpsec's systemd unit (`ntpsec`)          |
| `--dry-run`         | change nothing; log what it would do      |

`gps-station-desktop` takes `--radio`, `--gpsd` and `--chars` too. To
give it options, copy `/etc/xdg/autostart/gps-station-desktop.desktop` to
`~/.config/autostart/` and add them to its `Exec=` line.

The time job needs root (it reads gpsd's root-only shared memory and
edits `ntp.conf`); the service runs as root. The grid job alone needs no
privileges:

```sh
python3 src/gps_station.py --no-ntp
```
