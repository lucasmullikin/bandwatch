# tools/

Decoders that have no working Homebrew formula and have to be built. This
directory is gitignored — put the builds here, or anywhere on `PATH`, or point
`BANDWATCH_TOOLS` somewhere else.

Everything here is **optional**. Each one enables one lane; skip any you don't
want and leave that lane disabled in `config/lanes.json`.

`bootstrap.sh` installs the rest (`rtl-sdr`, `rtl_433`, `sox`, `socat`,
`ffmpeg`, `lame`, `mosquitto`, and optionally `direwolf`, `multimon-ng`,
`dump978`, `satdump`).

---

## RTLSDR-Airband — the voice lanes

The one most worth building. Without it there is no airband, no FRS/GMRS, no
MURS, no guard.

```sh
brew install cmake libconfig fftw libshout mp3lame
git clone https://github.com/charlie-foxtrot/RTLSDR-Airband
cd RTLSDR-Airband && mkdir build && cd build
cmake -DPLATFORM=native -DCMAKE_BUILD_TYPE=Release ..
make -j"$(sysctl -n hw.ncpu)"
```

`-DPLATFORM=native` matters — the default targets a Raspberry Pi and the build
either fails or produces something slower than it should be.

**Run it with `-F`.** Without that flag it daemonises: the supervisor sees an
instant exit while the real process escapes supervision *still holding the
dongle*, and the next lane to want that radio dies with
`usb_claim_interface error -3`. The lane script already passes it; this is here
because it will bite you if you test it by hand.

**It takes its device index from the config file, not from argv.** Scanning
process arguments for `-d 0` will tell you a radio is free while it is held.

## acarsdec — ACARS (aircraft text)

Does **not** build clean on macOS. `patches/acarsdec-macos.patch.sh` in this
repo documents what has to change; read it before you start rather than after.

```sh
brew install cmake libsndfile
git clone https://github.com/TLeconte/acarsdec
sh /path/to/bandwatch/patches/acarsdec-macos.patch.sh ./acarsdec
cd acarsdec && mkdir build && cd build
cmake -Drtl=ON .. && make
```

Worth the trouble: ACARS is **plain text**. No speech model, so no
transcription error is possible.

## dumpvdl2 — VDL2

The newer aircraft datalink, also text.

```sh
brew install cmake libglib-2.0
git clone https://github.com/szpajder/libacars && cd libacars
mkdir build && cd build && cmake .. && make && sudo make install
cd ../.. && git clone https://github.com/szpajder/dumpvdl2 && cd dumpvdl2
mkdir build && cd build && cmake -DRTLSDR=ON .. && make
```

Measured on the author's station: VDL2 arrived roughly **10 dB short** of a
usable decode on an indoor antenna. Do not read a silent VDL2 lane as a broken
build — check the sweep first.

## readsb or dump1090 — ADS-B

`bin/lane-adsb.sh` prefers `readsb` and falls back to `dump1090`. Either works.

```sh
brew install librtlsdr ncurses
git clone https://github.com/wiedehopf/readsb && cd readsb
make RTLSDR=yes
```

The lane taps the BaseStation port with `socat`, not `nc` — macOS `nc -d`
busy-spins on the socket and burned 95% of a core to move 658 bytes/sec. If you
change that, measure it.

---

## Checking what you have

```sh
./bootstrap.sh --check
```

Reports what is present and what is missing without installing anything. A lane
whose decoder is absent should be left `"enabled": false` in
`config/lanes.json` — an enabled lane that cannot start is a fault the watchdog
will keep trying to repair.
