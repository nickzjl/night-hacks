#!/bin/bash
# Starts the Night Hacks backend in macOS Terminal (double-click, or `open tools/run_backend.command`).
# It has to run from Terminal: macOS only lets a process use Bluetooth when the app that
# launched it has Bluetooth permission, and Terminal can ask you for it.
cd "$(dirname "$0")/.." || exit 1
lsof -ti tcp:8765 | xargs kill 2>/dev/null
sleep 1
echo "Night Hacks backend - dashboard at http://localhost:8765 (Ctrl+C to stop)"
.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8765 2>&1 | tee backend.log
