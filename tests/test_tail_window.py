"""
test_tail_window.py - tests for time-window slicing + parse progress.

Covers:
  - read_last_timestamp: tail-only timestamp lookup.
  - find_offset_for_timestamp: byte-aligned cutoff, including edge cases
    (cutoff before log start, cutoff after log end, log with no
    timestamps).
  - tail_file with start_offset: skips prefix correctly.
  - tail_file with progress_cb: invoked with monotonically growing byte
    positions, called at least once for a non-trivial file.
  - detect_combat with since=...: actually skips events older than the
    cutoff, regardless of whether the byte-offset finder undershoots.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from flurry.tail import (
    tail_file, read_last_timestamp, find_offset_for_timestamp,
)
from flurry import detect_fights


BASE = datetime(2026, 4, 25, 12, 0, 0)


def _line(off, body):
    ts = BASE + timedelta(seconds=off)
    return f'[{ts.strftime("%a %b %d %H:%M:%S %Y")}] {body}'


def _write_log(events):
    fd, path = tempfile.mkstemp(suffix='.txt', prefix='flurry_window_test_')
    os.close(fd)
    with open(path, 'w', newline='') as f:
        for off, body in events:
            f.write(_line(off, body) + '\r\n')
    return path


def _cleanup(path):
    try:
        os.unlink(path)
    except OSError:
        pass


# ----- read_last_timestamp -----

def test_read_last_timestamp_returns_latest_ts():
    path = _write_log([
        (0,    'You slash a goblin for 1 points of damage.'),
        (60,   'You slash a goblin for 2 points of damage.'),
        (3600, 'You slash a goblin for 3 points of damage.'),
    ])
    try:
        ts = read_last_timestamp(path)
        assert ts == BASE + timedelta(seconds=3600), ts
    finally:
        _cleanup(path)


def test_read_last_timestamp_returns_none_for_empty_file():
    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    try:
        assert read_last_timestamp(path) is None
    finally:
        _cleanup(path)


def test_read_last_timestamp_returns_none_when_no_timestamps_in_tail():
    """A file with body but no `[Day ...]` framing — happens for raw
    fragments, partial copies. Should not crash."""
    fd, path = tempfile.mkstemp(suffix='.txt')
    os.close(fd)
    with open(path, 'w') as f:
        f.write('this is not an EQ log\nno timestamps here\n')
    try:
        assert read_last_timestamp(path) is None
    finally:
        _cleanup(path)


# ----- find_offset_for_timestamp -----

def test_find_offset_lands_on_first_qualifying_line():
    """The returned offset, when fed to tail_file as start_offset,
    should yield the first line at-or-after the cutoff."""
    path = _write_log([(i, f'line-{i}') for i in range(0, 600, 10)])
    try:
        cutoff = BASE + timedelta(minutes=5)
        off = find_offset_for_timestamp(path, cutoff)
        first = next(tail_file(path, read_all=True, follow=False,
                               start_offset=off))
        assert 'line-300' in first, f'expected line-300 first, got: {first}'
    finally:
        _cleanup(path)


def test_find_offset_returns_zero_when_cutoff_is_before_log_start():
    path = _write_log([(i, f'line-{i}') for i in range(0, 60, 10)])
    try:
        cutoff = BASE - timedelta(hours=1)
        off = find_offset_for_timestamp(path, cutoff)
        assert off == 0
    finally:
        _cleanup(path)


def test_find_offset_returns_eof_when_cutoff_is_after_log_end():
    """If the cutoff is past every line, the offset lands at EOF and
    the caller iterates zero lines — desired for 'last 0s' edge cases."""
    path = _write_log([(i, f'line-{i}') for i in range(0, 60, 10)])
    try:
        cutoff = BASE + timedelta(hours=99)
        off = find_offset_for_timestamp(path, cutoff)
        size = os.path.getsize(path)
        assert off == size, f'expected EOF ({size}), got {off}'
        # Sanity: tail_file from EOF yields no lines.
        lines = list(tail_file(path, read_all=True, follow=False,
                               start_offset=off))
        assert lines == []
    finally:
        _cleanup(path)


def test_find_offset_for_timestamp_handles_chunk_boundaries():
    """Force the cutoff to fall in a region that needs multiple
    backward chunks. Regression test for the overlap logic."""
    # Generate enough lines that the file is several backscan-chunks
    # large. Each line is ~60 bytes; 2K lines = ~120KB > 64KB chunk.
    events = [(i, f'line-{i:05d}') for i in range(0, 2000)]
    path = _write_log(events)
    try:
        cutoff = BASE + timedelta(seconds=100)  # near the start
        off = find_offset_for_timestamp(path, cutoff)
        first = next(tail_file(path, read_all=True, follow=False,
                               start_offset=off))
        # Expect the first qualifying line to be line-00100 exactly.
        assert 'line-00100' in first
    finally:
        _cleanup(path)


# ----- tail_file with start_offset -----

def test_tail_file_start_offset_skips_prefix():
    path = _write_log([(i, f'line-{i}') for i in range(0, 60, 10)])
    try:
        off = find_offset_for_timestamp(path, BASE + timedelta(seconds=30))
        lines = list(tail_file(path, read_all=True, follow=False,
                               start_offset=off))
        # Lines 30, 40, 50 should remain.
        assert len(lines) == 3
        assert 'line-30' in lines[0]
    finally:
        _cleanup(path)


# ----- progress_cb -----

def test_tail_file_progress_callback_fires_at_least_once():
    """Need a file big enough to cross the progress_interval_bytes
    threshold (256KB by default). Generate ~600KB of lines."""
    events = [(i, 'X' * 200) for i in range(0, 3000)]
    path = _write_log(events)
    try:
        positions = []
        for _ in tail_file(path, read_all=True, follow=False,
                           progress_cb=lambda pos: positions.append(pos)):
            pass
        assert len(positions) >= 2, \
            f'expected multiple progress callbacks, got {len(positions)}'
        # Positions monotonically grow.
        for i in range(1, len(positions)):
            assert positions[i] >= positions[i - 1]
        # Final position equals file size (flush call at end of stream).
        assert positions[-1] == os.path.getsize(path)
    finally:
        _cleanup(path)


def test_tail_file_progress_callback_errors_swallowed():
    """A flaky observer shouldn't kill the parse mid-walk."""
    events = [(i, 'X' * 200) for i in range(0, 3000)]
    path = _write_log(events)
    try:
        def bad_cb(pos):
            raise RuntimeError('observer bug')
        # Should not raise.
        for _ in tail_file(path, read_all=True, follow=False,
                           progress_cb=bad_cb):
            pass
    finally:
        _cleanup(path)


# ----- detect_combat with since -----

def test_detect_combat_since_filters_old_events():
    """Events older than `since` must not contribute to fight
    accumulation, even if find_offset overshoots into them."""
    path = _write_log([
        (0, 'You slash old_mob for 99999 points of damage.'),
        (1, 'old_mob has been slain by You!'),
        (3600, 'You slash new_mob for 50000 points of damage.'),
        (3601, 'new_mob has been slain by You!'),
    ])
    try:
        cutoff = BASE + timedelta(minutes=30)
        fights = detect_fights(path, since=cutoff, min_damage=1)
        targets = [f.target for f in fights]
        assert targets == ['new_mob'], targets
    finally:
        _cleanup(path)


def test_detect_combat_progress_cb_fires_with_byte_pair():
    """progress_cb receives (bytes_read, slice_size). For a no-slice
    (since=None) parse, slice_size equals the file size."""
    events = [(i, 'X' * 200) for i in range(0, 3000)]
    path = _write_log(events)
    try:
        seen = []
        detect_fights(path,
                      progress_cb=lambda r, t: seen.append((r, t)),
                      min_damage=1)
        assert len(seen) >= 2
        size = os.path.getsize(path)
        for r, t in seen:
            assert t == size
        assert seen[-1][0] == size  # final tick at end of file
    finally:
        _cleanup(path)


def test_detect_combat_progress_cb_relative_to_slice_when_since_set():
    """When `since` skips a prefix, progress is reported relative to the
    slice — bar fills 0→100% across the work actually done, not jumping
    to two-thirds at start because the offset is two-thirds of the way
    in."""
    events = [(i, 'X' * 200) for i in range(0, 3000)]
    path = _write_log(events)
    try:
        seen = []
        # Cutoff at a timestamp that lands well into the file.
        cutoff = BASE + timedelta(seconds=2000)
        detect_fights(path, since=cutoff,
                      progress_cb=lambda r, t: seen.append((r, t)),
                      min_damage=1)
        if seen:
            size = os.path.getsize(path)
            for r, t in seen:
                # slice_size should be strictly less than the file size
                # because we sliced off a real prefix.
                assert t < size, f'slice_size {t} >= file size {size}'
                # bytes_read never exceeds slice_size.
                assert r <= t
    finally:
        _cleanup(path)


# ----- Manual entry point -----

if __name__ == '__main__':
    failures = 0
    tests = [v for k, v in globals().items()
             if k.startswith('test_') and callable(v)]
    for t in tests:
        try:
            t()
            print(f'  OK  {t.__name__}')
        except Exception as e:
            failures += 1
            print(f'  FAIL  {t.__name__}: {type(e).__name__}: {e}')
    print(f'\n{len(tests) - failures}/{len(tests)} passed')
    sys.exit(0 if failures == 0 else 1)
