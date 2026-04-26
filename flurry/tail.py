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
"""

import os
import time
from typing import Generator


def tail_file(path: str,
              read_all: bool = False,
              follow: bool = True,
              poll_interval: float = 0.25) -> Generator[str, None, None]:
    """Yield lines from `path`.

    Args:
      path: log file path.
      read_all: if True, yield every existing line first.
      follow: if True, after replay (or starting at EOF if read_all=False),
              keep watching for new lines indefinitely.
      poll_interval: how often (seconds) to check for new data when following.

    Yields:
      Each complete line, with line ending stripped.
    """
    # Open in binary mode so we can handle EQ's \r\n cleanly without Python
    # doing any newline translation that varies by platform. We decode to
    # text ourselves below.
    with open(path, 'rb') as f:
        if not read_all:
            f.seek(0, os.SEEK_END)

        buffer = b''
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
            else:
                if not follow:
                    if buffer:
                        yield buffer.decode('latin-1').rstrip('\r')
                    return
                time.sleep(poll_interval)
