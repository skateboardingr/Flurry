"""
tail.py - read lines from a log file, either historically or as written.

Two modes:

  * REPLAY mode (read_all=True): read the whole file from start to end, stop.
    Useful for offline analysis of past sessions.

  * LIVE mode (read_all=False, follow=True): seek to end-of-file and wait for
    new lines, yielding them as they arrive. Useful for live parse-as-you-play.

Both modes are exposed as generators, so the caller writes a simple
`for line in tail_file(...)` loop and doesn't have to care which is active.

Subtlety: EQ writes lines in chunks, and if we read mid-write we can get
a partial line (no trailing newline yet). We buffer until we see a newline
before yielding, so consumers always get whole lines.

Time-window helpers (`find_offset_for_timestamp`, `read_last_timestamp`)
let callers parse only the tail of a long log — useful when you only
care about tonight's raid in a multi-day file.
"""

import os
import re
import time
from datetime import datetime
from typing import Callable, Generator, Optional

from .parser import TIMESTAMP_RE, parse_timestamp


def tail_file(path: str,
              read_all: bool = False,
              follow: bool = True,
              poll_interval: float = 0.25,
              start_offset: int = 0,
              progress_cb: Optional[Callable[[int], None]] = None,
              progress_interval_bytes: int = 256 * 1024
              ) -> Generator[str, None, None]:
    """Yield lines from `path`.

    Args:
      path: log file path.
      read_all: if True, yield every existing line first.
      follow: if True, after replay (or starting at EOF if read_all=False),
              keep watching for new lines indefinitely.
      poll_interval: how often (seconds) to check for new data when following.
      start_offset: byte offset to seek to before reading. Must be
              line-aligned: 0 (default), or the byte immediately after
              a `\n` (which is what `find_offset_for_timestamp` returns).
              Combined with `read_all=True` this lets the caller skip
              the prefix of a long log. Ignored when `read_all=False`
              (we always go to EOF).
      progress_cb: optional callable invoked with the current absolute byte
              offset after every `progress_interval_bytes` of data. Lets the
              UI show parse progress during a long replay. Errors raised by
              the callback are swallowed so a flaky observer can't kill the
              parse mid-walk.
      progress_interval_bytes: minimum bytes between progress callbacks.
              Default 256KB — frequent enough for a smooth bar without
              flooding the observer.

    Yields:
      Each complete line, with line ending stripped.
    """
    # Open in binary mode so we can handle EQ's \r\n cleanly without Python
    # doing any newline translation that varies by platform. We decode to
    # text ourselves below.
    with open(path, 'rb') as f:
        if not read_all:
            f.seek(0, os.SEEK_END)
        elif start_offset > 0:
            # The contract is that start_offset is line-aligned, so we
            # just seek and start reading. Mis-aligned offsets surface
            # as a single garbled line at the head of the stream which
            # parse_line drops on the floor (no timestamp match).
            f.seek(start_offset)

        buffer = b''
        bytes_since_progress = 0
        while True:
            chunk = f.read(4096)
            if chunk:
                buffer += chunk
                # Split on \n. The last piece may be a partial line - keep
                # it in the buffer and prepend on next read.
                *complete, buffer = buffer.split(b'\n')
                for raw in complete:
                    # latin-1 won't fail on weird bytes; EQ logs are mostly
                    # ASCII but occasionally have stray chars.
                    yield raw.decode('latin-1').rstrip('\r')
                bytes_since_progress += len(chunk)
                if (progress_cb is not None
                        and bytes_since_progress >= progress_interval_bytes):
                    try:
                        progress_cb(f.tell())
                    except Exception:
                        pass
                    bytes_since_progress = 0
            else:
                if not follow:
                    if buffer:
                        yield buffer.decode('latin-1').rstrip('\r')
                    if progress_cb is not None:
                        try:
                            progress_cb(f.tell())
                        except Exception:
                            pass
                    return
                time.sleep(poll_interval)


# ----- Time-window helpers -----
#
# Both helpers operate at the byte level so they stay fast on huge logs:
# we read tail-end chunks rather than the whole file, and we look for the
# leading `[Day Mon DD HH:MM:SS YYYY]` timestamp pattern with a regex
# match rather than parsing every line into structured form.

# Compile once. Same shape as parser.TIMESTAMP_RE but with no body group
# and no anchors so we can scan arbitrary chunks.
_TS_SCAN_RE = re.compile(
    rb'\[([A-Z][a-z]{2} [A-Z][a-z]{2} ?\d{1,2} \d{2}:\d{2}:\d{2} \d{4})\]'
)

# How big a chunk to read at a time when scanning backwards from EOF.
# Big enough that most reasonable lookback ranges land in one or two reads,
# small enough that we don't pull more than necessary on huge logs.
_BACKSCAN_CHUNK = 64 * 1024


def read_last_timestamp(path: str,
                        max_tail_bytes: int = 4 * 1024 * 1024
                        ) -> Optional[datetime]:
    """Return the timestamp on the latest line of the log, or None if no
    timestamp is found in the last `max_tail_bytes` bytes.

    Reads only the tail of the file, so it's fast even on multi-gig logs.
    Useful for computing a "since" cutoff anchored to log-end rather than
    wall clock — important when analyzing yesterday's raid.
    """
    size = os.path.getsize(path)
    if size == 0:
        return None
    read_size = min(size, max_tail_bytes)
    with open(path, 'rb') as f:
        f.seek(size - read_size)
        data = f.read(read_size)
    # Find the LAST timestamp match in the chunk. iterate to avoid
    # building a list when matches are dense.
    last = None
    for m in _TS_SCAN_RE.finditer(data):
        last = m
    if last is None:
        return None
    try:
        return parse_timestamp(last.group(1).decode('latin-1'))
    except ValueError:
        return None


def find_offset_for_timestamp(path: str,
                              since: datetime
                              ) -> int:
    """Return the byte offset of the earliest line with a timestamp at or
    after `since`. Returns 0 if `since` is before the log's first line,
    or `os.path.getsize(path)` if `since` is after the log's last line
    (caller will then read nothing — desired behavior).

    Implementation: scan backwards from EOF in 64KB chunks, looking for
    the LAST timestamp older than `since`. The byte offset of the next
    newline after that timestamp is our answer. This avoids the worst
    case of bisecting a sparse-timestamp file where mid-file seeks land
    in non-line-leading positions.
    """
    size = os.path.getsize(path)
    if size == 0:
        return 0

    # We slide a window backwards. Keep some overlap between chunks so a
    # timestamp that straddles a chunk boundary still gets found (a
    # timestamp is at most ~28 bytes; 64-byte overlap is plenty).
    overlap = 64
    pos = size
    earliest_match_offset_in_window: Optional[int] = None
    earliest_after_since_abs: Optional[int] = None

    while pos > 0:
        read_start = max(0, pos - _BACKSCAN_CHUNK)
        with open(path, 'rb') as f:
            f.seek(read_start)
            data = f.read(pos - read_start)
        # Walk timestamps in this chunk, scanning right-to-left so we hit
        # the latest matches first. We're looking for the latest line
        # that's still BEFORE `since`; the answer offset is the next
        # newline after it.
        matches = list(_TS_SCAN_RE.finditer(data))
        # Iterate from latest to earliest within the chunk.
        for m in reversed(matches):
            try:
                ts = parse_timestamp(m.group(1).decode('latin-1'))
            except ValueError:
                continue
            abs_offset = read_start + m.start()
            if ts < since:
                # This line is the last one BEFORE since. Return the
                # offset just past it (next newline).
                # The timestamp at abs_offset starts the line that's
                # too old. Skip past this line's newline.
                nl = data.find(b'\n', m.end())
                if nl == -1:
                    # The line continues past our chunk; the start of the
                    # next chunk we read forward from `read_start` would
                    # contain it. Read the file directly to find the nl.
                    with open(path, 'rb') as f:
                        f.seek(abs_offset)
                        # Read enough to find the newline of this line.
                        line_data = f.readline()
                        return abs_offset + len(line_data)
                return read_start + nl + 1
            else:
                earliest_after_since_abs = abs_offset
        # No timestamp older than `since` in this chunk; keep scanning
        # backwards. Advance pos with overlap to catch boundary-straddling
        # timestamps.
        if read_start == 0:
            break
        pos = read_start + overlap

    # We never found a line older than since → since is before the log
    # starts. Either the earliest seen line is at offset 0 already, or
    # we have nothing useful, but in either case the right answer is the
    # very start of the file.
    if earliest_after_since_abs is not None:
        return earliest_after_since_abs
    return 0
