#!/usr/bin/env bash
# flurry.sh - launch Flurry from the source tree on macOS / Linux.
# Run from a terminal: `./flurry.sh [logfile]`.
# Make executable once with: chmod +x flurry.sh
cd "$(dirname "$0")"
python3 -m flurry "$@"
