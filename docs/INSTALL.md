# Installing and configuring gps-station

See the [README](../README.md) for what gps-station does.

## Requirements

- An sBitx or zBitx on Raspberry Pi OS (Bullseye or later; 32- or 64-bit)
- A GPS receiver that gpsd can read
- `gpsd`, `python3` (3.9 or later), and `ntpsec` for the time job
- For the time job, gpsd started with `-n`, so it feeds time to shared
  memory (see below)

The package is architecture `all`: the same `.deb` installs on 32-bit
(armhf) and 64-bit (arm64) systems.

## Install

### From the apt repository

```sh
sudo curl -fsSL -o /usr/share/keyrings/gps-station.gpg \
    https://jchonig.github.io/bitx-gps-station/gps-station.gpg
echo "deb [signed-by=/usr/share/keyrings/gps-station.gpg] \
https://jchonig.github.io/bitx-gps-station stable main" |
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

### gpsd must run with `-n` for the time job

Without `-n`, gpsd hands fixes to programs that ask but never writes time
to the shared memory ntpsec reads, and the GPS never becomes a time
source. Add it in `/etc/default/gpsd`:

```sh
GPSD_OPTIONS="-n"
```

then `sudo systemctl restart gpsd`. If it is missing, the service says
so in its log once it has a fix (`journalctl -u gps-station`). The
package doesn't change gpsd's settings itself.

## Configuration

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
