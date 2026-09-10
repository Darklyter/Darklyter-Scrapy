"""Integrity checks for finished downloads.

A download can fail in ways that still leave a plausible-looking file: AdultTime
serves stub files once the daily allowance is spent, and a dropped connection
leaves a truncated but otherwise valid-looking video.  These checks run after a
file lands so the result can be recorded rather than discovered months later.

Two signals are used, cheapest first:

* size against what AdultTime said the file would be -- the strongest check for
  truncation, and free.
* structural validity: ffprobe for video, the zip central directory for
  photosets.  Both read headers rather than decoding, so they cost little even
  on a multi-gigabyte file.

Nothing here decodes a whole file; the goal is to catch broken downloads, not to
detect subtle corruption in the middle of an otherwise complete video.
"""

import json
import os
import subprocess
import zipfile

# A download whose size is within this fraction of the advertised size is
# accepted.  Containers differ by a few bytes between what the CDN reports and
# what lands, so an exact match is too strict.
SIZE_TOLERANCE = 0.02

# AdultTime answers with a tiny stub instead of a video once the daily cap is
# spent.  Anything this small is not a real download.
STUB_SIZE_LIMIT = 20000

FFPROBE_TIMEOUT = 120

STATUS_OK = 'ok'
STATUS_TRUNCATED = 'truncated'
STATUS_CORRUPT = 'corrupt'
STATUS_STUB = 'stub'
STATUS_MISSING = 'missing'
STATUS_UNCHECKED = 'unchecked'


def check_size(path, expected_bytes):
    """Compare the file on disk against the size AdultTime advertised."""
    actual = os.path.getsize(path)
    if actual < STUB_SIZE_LIMIT:
        return STATUS_STUB, f'{actual} bytes, below the {STUB_SIZE_LIMIT} stub threshold'
    if expected_bytes:
        expected = int(expected_bytes)
        if expected and actual < expected * (1 - SIZE_TOLERANCE):
            short = 100.0 * (1 - actual / expected)
            return STATUS_TRUNCATED, f'{actual} of {expected} bytes ({short:.1f}% short)'
    return STATUS_OK, f'{actual} bytes'


def verify_video(path, expected_bytes=None):
    """Return (status, detail) for a downloaded video."""
    if not os.path.exists(path):
        return STATUS_MISSING, 'file not found'

    status, detail = check_size(path, expected_bytes)
    if status != STATUS_OK:
        return status, detail

    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries',
             'format=duration:stream=codec_type', '-of', 'json', path],
            capture_output=True, timeout=FFPROBE_TIMEOUT)
    except FileNotFoundError:
        return STATUS_UNCHECKED, 'ffprobe not installed'
    except subprocess.TimeoutExpired:
        return STATUS_CORRUPT, 'ffprobe timed out'

    if result.returncode != 0:
        message = result.stderr.decode('utf8', 'replace').strip().splitlines()
        return STATUS_CORRUPT, message[-1][:200] if message else 'ffprobe failed'

    try:
        probe = json.loads(result.stdout or b'{}')
    except ValueError:
        return STATUS_CORRUPT, 'ffprobe returned unparseable output'

    streams = probe.get('streams') or []
    if not any(stream.get('codec_type') == 'video' for stream in streams):
        return STATUS_CORRUPT, 'no video stream'

    duration = (probe.get('format') or {}).get('duration')
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        # Some containers omit a duration in the header even when intact, so
        # this alone is not grounds for calling the file corrupt.
        return STATUS_OK, f'{detail}, no duration reported'
    if seconds <= 0:
        return STATUS_CORRUPT, 'zero duration'

    return STATUS_OK, f'{detail}, {seconds / 60:.1f} min'


def verify_gallery(path, expected_bytes=None):
    """Return (status, detail) for a downloaded photoset zip."""
    if not os.path.exists(path):
        return STATUS_MISSING, 'file not found'

    status, detail = check_size(path, expected_bytes)
    if status != STATUS_OK:
        return status, detail

    try:
        with zipfile.ZipFile(path) as archive:
            broken = archive.testzip()
            if broken is not None:
                return STATUS_CORRUPT, f'bad CRC on {broken}'
            count = len(archive.namelist())
    except zipfile.BadZipFile as error:
        return STATUS_CORRUPT, str(error)[:200]
    except OSError as error:
        return STATUS_CORRUPT, f'unreadable: {error}'

    if not count:
        return STATUS_CORRUPT, 'archive is empty'
    return STATUS_OK, f'{detail}, {count} entries'


def verify(path, is_gallery, expected_bytes=None):
    """Dispatch to the right check for the file type."""
    if is_gallery:
        return verify_gallery(path, expected_bytes)
    return verify_video(path, expected_bytes)
