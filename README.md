# bitx-gps-station

Sets the clock and the grid square from a GPS on an sBitx or zBitx, running
either the original sbitx software or the refactored zbitxre, and keeps
the ham programs running on its desktop on the same grid square.

It has two parts, in one package:

- `gps-station`: a system service for the clock and the radio's grid
- `gps-station-desktop`: a tray icon that watches WSJT-X, JS8Call and
  the other programs running on the desktop; it starts when you log in
  to the desktop, so a radio without one simply doesn't run it

## What it does

`gps-station` runs as a systemd service beside `gpsd` and does two jobs.

**Time.** As soon as gpsd starts writing time to shared memory (the
receiver has a fix), it adds a `refclock shm` line to
`/etc/ntpsec/ntp.conf` and restarts ntpsec. Later, when ntpd is synced to
a network server and has heard the GPS steadily, it measures how late the
GPS's NMEA time arrives over ten polls, sets that as `time1`, restarts
ntpsec once more, and leaves it alone from then on. With the network up,
internet servers outvote the GPS; in the field, the GPS keeps the clock.

Its lines in `ntp.conf` sit between `# gps_station: begin` and
`# gps_station: end` markers. If `ntp.conf` already has a `refclock shm`
line that it did not write, it leaves ntpsec alone.

**Grid.** It works out the 6-character (or, with `--chars 4`,
4-character) Maidenhead locator from the fix and sets it on the radio over
TCP port 8081, retrying until the radio program answers and the new value
reads back:

| Radio software      | Command sent    | Read back         |
| ------------------- | --------------- | ----------------- |
| zbitxre             | `MYGRID FN31pr` | greeting `MYGRID` |
| sbitx (JJ's, W9JES) | `grid FN31pr`   | `?MYGRID`         |

A new grid is sent only after it has held for a minute, so a fix
wandering along a grid line doesn't flip it back and forth, and a grid you
type on the radio yourself is left alone until you move.

## The desktop programs

The tray icon starts when you log in to the radio's desktop and
puts a GPS icon in the panel's tray: green when every ticked program has
the GPS grid, amber when one doesn't, grey with no fix. Clicking it opens
a window, kept off the taskbar, showing the GPS grid, the receiver's
state (no fix, 2D or 3D fix; satellites used, heard with any signal and
in view; the strongest signal) and a row for each supported program that
is running. The window stays above the radio's full-screen window:

```text
GPS: FN31pr
3D fix: 7 used, 9 heard of 11 in view (best 41 dB-Hz)
[x] Radio (zbitxre)  ✓ FN31pr                  [Update]
[x] WSJT-X           ✗ AA11AA stale            [Update]
[x] JS8Call          ✋ FN42aa set by hand     [Update]
[ ] fldigi           ? can't read              [Update]
```

Only programs that are running are looked at; a closed program is left
alone, because the radio may have moved by the time it is opened again.
Every 10 seconds each row is marked:

| Mark | Meaning                                                            |
| ---- | ------------------------------------------------------------------ |
| ✓    | it has the GPS grid                                                |
| ✗    | stale: it hasn't had the GPS grid since it started or the GPS moved |
| ✋   | set by hand: it had the GPS grid, then somebody changed it         |
| ?    | it can't report its grid while running                             |

Only ticked programs are changed. A ticked program with a stale grid is
updated; a grid set by hand is kept until the GPS moves to another square
or you press **Update**, which sends the GPS grid at once, ticked or not.
When a ticked program can't be updated, a desktop notification says why,
once each time the program is started. The ticks are kept in
`~/.config/gps-station/apps.json`; a program seen for the first time
starts unticked.

What each program allows while it is running:

| Program     | Read  | Set   | Settings needed in the program               |
| ----------- | ----- | ----- | -------------------------------------------- |
| Radio       | yes   | yes   | none                                         |
| WSJT-X      | yes   | yes   | Accept UDP requests; AutoGrid next to My Grid |
| JS8Call     | yes   | yes   | TCP server API on; Accept TCP requests        |
| JTDX        | yes   | no    | none; set My Grid by hand                    |
| fldigi      | no    | no    | none; check Configure, Operator              |
| QSSTV       | no    | no    | none; check its configuration                |
| GridTracker | —     | —     | follows WSJT-X / JTDX                        |

WSJT-X and JTDX are heard on the UDP address they send to (Settings,
Reporting, UDP Server). GridTracker normally listens on that address too,
and only one program can listen on a plain address such as
`127.0.0.1:2237`; the WSJT-X row then says the port is taken. To share
it, use a multicast address, which any number of programs can hear:

1. WSJT-X (and JTDX): Settings, Reporting: UDP Server `239.255.0.1`,
   port `2237`, Outgoing interfaces `lo`.
2. GridTracker2: turn on Multicast and give it the same address.

Linux leaves multicast off on the loopback interface, so WSJT-X doesn't
offer `lo` until it is on. The package's `gps-station-lo-multicast`
service turns it on at boot (`ip link set dev lo multicast on`); using
`lo` keeps the stream on the radio and working without Wi-Fi. Restart
WSJT-X after the first install so it sees the change.

gps-station-desktop reads the address from WSJT-X's settings and joins
the group on every interface, so it needs no setting of its own.

The radio's row reads the radio over port 8081; the radio is kept up to
date by the `gps-station` service whether or not anyone is logged in.

## Requirements

- An sBitx or zBitx on Raspberry Pi OS (Bullseye or later; 32- or 64-bit)
- A GPS receiver that gpsd can read
- `gpsd`, `python3` (3.9 or later), and `ntpsec` for the time job

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

```sh
sudo install -D -m 755 src/gps_station.py /usr/lib/gps-station/gps_station.py
sudo ln -s ../lib/gps-station/gps_station.py /usr/sbin/gps-station
sudo install -m 644 debian/gps-station.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now gps-station
```

## Configuration

Options go in `GPS_STATION_ARGS` in `/etc/default/gps-station`; restart
the service after changing it.

| Option              | Effect                                        |
| ------------------- | --------------------------------------------- |
| `--chars 4`         | send a 4-character grid instead of 6          |
| `--no-ntp`          | only set the grid; leave ntpsec alone         |
| `--no-grid`         | only look after ntpsec                        |
| `--radio HOST:PORT` | radio software's port (`127.0.0.1:8081`)      |
| `--gpsd HOST:PORT`  | gpsd's port (`127.0.0.1:2947`)                |
| `--conf PATH`       | ntpsec config (`/etc/ntpsec/ntp.conf`)        |
| `--service NAME`    | ntpsec's systemd unit (`ntpsec`)              |
| `--dry-run`         | change nothing; log what it would do          |

`gps-station-desktop` takes `--radio`, `--gpsd` and `--chars` too. To
give it options, copy `/etc/xdg/autostart/gps-station-desktop.desktop` to
`~/.config/autostart/` and add them to its `Exec=` line.

The time job needs root (it reads gpsd's root-only shared memory and
edits `ntp.conf`); the service runs as root. The grid job alone needs no
privileges:

```sh
python3 src/gps_station.py --no-ntp
```

## Development

Everything runs in Docker through the Makefile; nothing is installed on
the host.

| Target       | Does                                               |
| ------------ | -------------------------------------------------- |
| `make lint`  | ruff, shellcheck, markdownlint, actionlint         |
| `make test`  | pytest on Debian Bookworm's Python                 |
| `make deb`   | builds and lintian-checks the `.deb` in `dist/`    |
| `make repo`  | builds the apt repository in `public/` from `dist/` |
| `make hooks` | runs `make lint` as a git pre-commit hook          |

Run `make hooks` once per clone.

GitHub Actions runs lint, tests and the package build on every push to
`main` and on pull requests, and keeps the `.deb` as a build artifact.

## Releasing

1. Add an entry to `debian/changelog` with the new version.
2. Commit, then tag and push: `git tag v0.2.0 && git push origin v0.2.0`.

The release workflow checks the tag against `debian/changelog`, lints,
tests, builds the `.deb`, attaches it to a GitHub Release, and publishes
the signed apt repository to GitHub Pages. The repository serves the
latest version; older ones remain on the Releases page.

One-time setup:

1. Make a signing key with no passphrase, used only for this repository:

   ```sh
   gpg --quick-gen-key "bitx-gps-station apt <jch@honig.net>" \
       ed25519 sign never
   gpg --armor --export-secret-keys "bitx-gps-station apt"
   ```

2. Store the armored private key as the repository secret
   `APT_SIGNING_KEY` (Settings → Secrets and variables → Actions).
3. Under Settings → Pages, set Source to **GitHub Actions**.
4. Under Settings → Environments → github-pages, allow tags matching
   `v*` to deploy.

## License

BSD 3-Clause; see [LICENSE](LICENSE).
