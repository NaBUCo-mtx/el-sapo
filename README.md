# El Sapo

Raspberry Pi host software for El Sapo's WiFi/BLE/RFID recon Toadlets — a
FastAPI dashboard that talks to ESP32-based WiFi/BT Toadlets over UART and
an M7E Hecto UHF RFID Toadlet over USB-serial, streaming parsed events live
over WebSocket and logging them to disk.

## Prerequisites

- Raspberry Pi OS Lite (64-bit) with `git` and Python 3.11+.
- The Toadlets wired up: WiFi/BT ESP32s on UART (`/dev/ttyAMA2`,
  `/dev/ttyAMA3` by default), RFID reader on USB-serial (`/dev/ttyUSB0` by
  default). Wiring and any UART device-tree overlays your board needs are
  hardware-specific.
- The service user in the `dialout` group for serial access:
  `sudo usermod -aG dialout <user>`.

## Install

```bash
git clone <this-repo-url> ~/el-sapo
cd ~/el-sapo
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -e .
```

## Configure

Copy the example config and edit device paths, baud rates, and offline
thresholds to match your hardware:

```bash
cp config/example.toml config/default.toml
```

Set `source = "mock"` on any toadlet in the config to run without that
piece of hardware attached — useful for a first smoke test. In mock mode
a simulated toadlet emits realistic-cadence fake events instead.

## Run

```bash
.venv/bin/cyberdeck-pi --config config/default.toml
```

Then browse to `http://<pi-hostname-or-ip>:8080` from any device on the
same network.

## Install as a systemd service

```bash
sudo cp systemd/cyberdeck-pi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cyberdeck-pi
```

Edit the `WorkingDirectory`, `ExecStart`, and `User`/`Group` fields in the
unit file first if your clone path or service user differs from the
defaults (`/home/deck/CyberDeck_Pi`, user `deck`, group `dialout`).

Check status and logs with:

```bash
sudo systemctl status cyberdeck-pi
journalctl -u cyberdeck-pi -f
```
