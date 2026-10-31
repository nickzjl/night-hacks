# Night Hacks

Our Night Hacks hackathon project.

> **Status:** Tech stack and project structure are still being decided. No application code yet.

## Getting started

```bash
git clone https://github.com/nickzjl/night-hacks.git
cd night-hacks
git config pull.rebase true
```

## Workflow

We both commit straight to `main`. No PRs or reviews needed.

1. `git pull` before you start working.
2. Commit small and often.
3. `git pull` again, then `git push`.

`pull.rebase` is set to `true`, so pulling replays your commits on top of your teammate's and avoids merge commits. If a rebase hits a conflict, fix the files, `git add` them, and run `git rebase --continue`.

## Running the demo

```
iPhone app (ARKit LiDAR + haptics) --Bluetooth LE or Wi-Fi/USB--> Mac backend (Claude + Braille) --USB serial--> Feather (CircuitPython) --> 6 servos
```

1. **Backend** (Mac): `python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.txt`, put `ANTHROPIC_API_KEY` in `backend/.env` (see `backend/.env.example`), then `open tools/run_backend.command`. It runs in macOS Terminal because Bluetooth needs Terminal's permission (allow it once). Dashboard: http://localhost:8765
2. **iPhone**: plug it in, unlock, then `tools/run_ios.sh` (builds, installs, launches). Allow Camera, Local Network and Bluetooth. The app connects over Bluetooth on its own; Wi-Fi/USB is used instead when it can reach the Mac (the venue Wi-Fi blocks this). Proximity vibration runs on the phone and never needs the Mac.
3. **Braille cell**: copy `firmware/code.py` to the Feather's `CIRCUITPY` drive and plug it into the Mac. The backend finds the board automatically (`SERIAL_PORT=auto`).

No hardware yet? Run `.venv/bin/python tools/fake_feather.py`; `SERIAL_PORT=auto` uses it until the real board is plugged in, then switches. No phone? Click **Use Mac webcam as phone** on the dashboard.

**Serial protocol:** one line per cell, two hex digits + newline (`19\n` = dots 1-4-5 = D). bit0 = dot 1 … bit5 = dot 6. The board replies `OK 19`. Letter timing (`HOLD_MS`, `GAP_MS`, `WORD_GAP_MS`) lives on the Mac.

**Firmware calibration** (top of `firmware/code.py`): servo pins `D5 D6 D9 D10 D11 D12` = dots 1–6, `DOWN`/`UP` angles per dot. Servos get their own 5 V ≥3 A supply with ground tied to the Feather. Type `3F` + Enter in a serial terminal to raise all dots, `T` for a self-test.

## Environment variables

Keep secrets in `.env`, which Git ignores. Commit a `.env.example` with the variable names but no real values.
