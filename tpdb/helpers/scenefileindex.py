"""
Index of already-downloaded scene files, used to decide what still needs ripping.

Walking ~600k files across three network mounts takes minutes, so the walk result
is parsed once into a SQLite database and reused on later runs.  Refresh it with
``-a reindex=1`` on the spider, or let it rebuild itself once it goes stale.

Two filename grammars are recognised:

  A  ``<site> - YYYY-MM-DD - <title>.ext``
     "Devils Film - 2012-07-03 - We Are Fucking With Our Neighbors #03.mkv"

  B  ``<site> YYYY-MM-DD <title> [(<performers>)] [<id>] [<resolution>].ext``
     "Girlsway 2026-04-16 Three Sitters, One Slot (Hazel Moore) [285662] [1920x1080].mkv"

In grammar B the *last* bracket group is always the resolution; a scene id, when
present at all, is the group before it.  Roughly a third of the Website_Organized
files carry no id bracket, so id matching alone is not enough and titles have to
be compared too.
"""

import os
import re
import sqlite3
import time

from unidecode import unidecode

# Media we care about.  '.zip' is in here because galleries are ripped alongside
# the videos and deduped against the same index.
VIDEO_EXTENSIONS = {
    'mp4', 'mkv', 'wmv', 'avi', 'm4v', 'mov', 'mpg', 'mpeg', 'ts', 'flv', 'rm',
}
GALLERY_EXTENSIONS = {'zip'}
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | GALLERY_EXTENSIONS

# Directories that are storage-appliance noise rather than content.
SKIP_DIRECTORIES = {'@eaDir', '.recycle', '#recycle', '@Recycle', '.@__thumb'}

DATE_PATTERN = r'(?P<date>(?:19|20)\d{2}-\d{2}-\d{2})'

# "Site - 2012-07-03 - Title"
GRAMMAR_A = re.compile(r'^(?P<site>.+?)\s+-\s+' + DATE_PATTERN + r'\s+-\s+(?P<rest>.+)$')
# "Site 2026-04-16 Title (Performers) [id] [1920x1080]"
GRAMMAR_B = re.compile(r'^(?P<site>.+?)\s+' + DATE_PATTERN + r'\s+(?P<rest>.+)$')

TRAILING_BRACKET = re.compile(r'\s*\[([^\[\]]*)\]\s*$')
TRAILING_PARENS = re.compile(r'\s*\(([^()]*)\)\s*$')
SLUG_TRAILING_ID = re.compile(r'[-_](\d+)$')

# A scene id extracted from a slug needs at least this many digits before we
# trust it.  Without the floor, a MetArt name like "..._red_hot_pepper_2" would
# register as scene id 2 and shadow a real AdultTime clip.
MIN_SLUG_ID_DIGITS = 4

# AdultTime clip ids run to six digits.  Anything much longer is some other
# site's internal key -- Bang, for one, uses 24-digit ids that do not even fit
# in a SQLite INTEGER -- so treat those as "no id" rather than indexing them.
MAX_ID_DIGITS = 10


def normalize(value):
    """Fold a title down to the form used for comparisons.

    The three trees were named by different tools over many years, so the same
    scene shows up as "Emma Rose & Hatler Gurius" in one place and
    "Emma Rose And Hatler Gurius" in another, and as "#06" / "06" / "6".
    Everything that varies cosmetically gets flattened away here.
    """
    if not value:
        return ''
    value = unidecode(value).lower()
    value = value.replace('&', ' and ')
    # "Vol 4" / "Volume 4" are the same series entry.
    value = re.sub(r'\bvolume\b', 'vol', value)
    # Compare digit runs numerically so "#06", "06" and "6" collapse together.
    value = re.sub(r'\d+', lambda match: str(int(match.group())), value)
    return re.sub(r'[^a-z0-9]+', '', value)


def extract_scene_id(bracket_text):
    """Return the numeric scene id inside a bracket group, if there is one."""
    text = bracket_text.strip()
    if text.isdigit():
        return int(text) if len(text) <= MAX_ID_DIGITS else None
    match = SLUG_TRAILING_ID.search(text)
    if match and MIN_SLUG_ID_DIGITS <= len(match.group(1)) <= MAX_ID_DIGITS:
        return int(match.group(1))
    return None


def _strip_trailing_brackets(text):
    """Peel trailing ``[...]`` groups off, returning (remainder, groups)."""
    groups = []
    while True:
        match = TRAILING_BRACKET.search(text)
        if not match:
            return text.strip(), groups
        groups.insert(0, match.group(1))
        text = text[:match.start()]


def parse_filename(filename):
    """Parse one filename into its scene fields, or return None if it doesn't fit.

    Returns a dict with ``site``, ``date``, ``title``, ``title_no_performers``
    (None when the name carries no trailing performer group), ``scene_id`` and
    ``extension``.
    """
    stem, _, extension = filename.rpartition('.')
    extension = extension.lower()
    if not stem or extension not in MEDIA_EXTENSIONS:
        return None

    match = GRAMMAR_A.match(stem)
    if match:
        # Grammar A has no resolution bracket, so a trailing group is the scene
        # id and nothing else.  Older rips carry no bracket at all; ones this
        # spider writes end in "[clip_id]".  The id is kept out of the title so
        # both generations still compare equal on date+title.
        rest, brackets = _strip_trailing_brackets(match.group('rest'))
        scene_id = extract_scene_id(brackets[-1]) if brackets else None
        title = rest
        title_no_performers = None
    else:
        match = GRAMMAR_B.match(stem)
        if not match:
            return None
        rest, brackets = _strip_trailing_brackets(match.group('rest'))
        scene_id = None
        if brackets:
            brackets.pop()  # the final group is the resolution
        if brackets:
            scene_id = extract_scene_id(brackets[-1])
        title = rest
        performer_match = TRAILING_PARENS.search(title)
        # Titles can legitimately end in parentheses ("Looking Up(skirt)"), so
        # keep both readings and let the matcher try each.
        title_no_performers = title[:performer_match.start()].strip() if performer_match else None

    if not title:
        return None

    return {
        'site': match.group('site').strip(),
        'date': match.group('date'),
        'title': title,
        'title_no_performers': title_no_performers,
        'scene_id': scene_id,
        'extension': extension,
    }


def walk_media_files(root):
    """Yield (full_path, filename) for every media file under root."""
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            try:
                if entry.is_dir(follow_symlinks=False):
                    if entry.name not in SKIP_DIRECTORIES:
                        stack.append(entry.path)
                elif entry.is_file(follow_symlinks=False):
                    yield entry.path, entry.name
            except OSError:
                continue


class SceneFileIndex:
    """SQLite-backed lookup of scenes already present on disk."""

    SCHEMA = """
        CREATE TABLE IF NOT EXISTS files (
            path        TEXT PRIMARY KEY,
            root        TEXT NOT NULL,
            filename    TEXT NOT NULL,
            site        TEXT,
            date        TEXT,
            title_norm  TEXT,
            alt_norm    TEXT,
            scene_id    INTEGER,
            is_gallery  INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS files_by_id    ON files (scene_id, date);
        CREATE INDEX IF NOT EXISTS files_by_title ON files (date, title_norm);
        CREATE INDEX IF NOT EXISTS files_by_alt   ON files (date, alt_norm);
        CREATE TABLE IF NOT EXISTS index_meta (
            key   TEXT PRIMARY KEY,
            value TEXT
        );
        -- Downloads this spider performed, with the result of the integrity
        -- check.  Survives a rebuild of `files`, which is wiped and re-walked
        -- every run, so it is the only lasting record of what was fetched and
        -- whether it arrived intact.
        CREATE TABLE IF NOT EXISTS downloads (
            path           TEXT PRIMARY KEY,
            scene_id       INTEGER,
            is_gallery     INTEGER NOT NULL DEFAULT 0,
            filename       TEXT,
            bytes          INTEGER,
            expected_bytes INTEGER,
            status         TEXT,
            detail         TEXT,
            downloaded_at  REAL
        );
        CREATE INDEX IF NOT EXISTS downloads_by_id ON downloads (scene_id, is_gallery);
        CREATE INDEX IF NOT EXISTS downloads_by_status ON downloads (status);
    """

    def __init__(self, database_path, roots, date_slack=1, logger=None):
        self.database_path = database_path
        self.roots = list(roots)
        self.date_slack = int(date_slack)
        self.log = logger or (lambda message: print(message))
        directory = os.path.dirname(database_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self.connection = sqlite3.connect(database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(self.SCHEMA)

    # -- building ---------------------------------------------------------

    def age_in_days(self):
        """Days since the last successful build, or None if never built."""
        row = self.connection.execute(
            "SELECT value FROM index_meta WHERE key = 'built_at'").fetchone()
        if not row:
            return None
        return (time.time() - float(row['value'])) / 86400.0

    def is_empty(self):
        return self.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0] == 0

    def build(self):
        """Re-walk every root and rebuild the index from scratch."""
        started = time.time()
        self.connection.execute("DELETE FROM files")
        total_seen = total_parsed = 0

        for root in self.roots:
            if not os.path.isdir(root):
                self.log(f"Scene index: skipping missing root {root}")
                continue
            root_started = time.time()
            seen = parsed = 0
            batch = []
            for path, filename in walk_media_files(root):
                seen += 1
                fields = parse_filename(filename)
                if not fields:
                    continue
                parsed += 1
                alt = fields['title_no_performers']
                batch.append((
                    path,
                    root,
                    filename,
                    fields['site'],
                    fields['date'],
                    normalize(fields['title']),
                    normalize(alt) if alt else None,
                    fields['scene_id'],
                    1 if fields['extension'] in GALLERY_EXTENSIONS else 0,
                ))
                if len(batch) >= 5000:
                    self._insert(batch)
                    batch = []
            if batch:
                self._insert(batch)
            total_seen += seen
            total_parsed += parsed
            self.log(f"Scene index: {root} -> {parsed}/{seen} files parsed "
                     f"in {time.time() - root_started:.0f}s")

        self.connection.execute(
            "INSERT OR REPLACE INTO index_meta (key, value) VALUES ('built_at', ?)",
            (str(time.time()),))
        self.connection.commit()
        self.log(f"Scene index: {total_parsed}/{total_seen} files indexed "
                 f"in {time.time() - started:.0f}s -> {self.database_path}")
        return total_parsed

    def _insert(self, batch):
        self.connection.executemany(
            "INSERT OR REPLACE INTO files "
            "(path, root, filename, site, date, title_norm, alt_norm, scene_id, is_gallery) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)

    def ensure_built(self, force=False, max_age_days=0):
        """Build the index unless an existing one is young enough to trust.

        Defaults to rebuilding every time.  The walk costs well under a minute
        against hours of downloading, and a stale index is actively harmful: it
        cannot see files the previous run downloaded, so those get fetched all
        over again.  Pass max_age_days only to reuse an index for quick tests.
        """
        if force or self.is_empty() or max_age_days <= 0:
            return self.build()
        age = self.age_in_days()
        if age is None or age > max_age_days:
            return self.build()
        self.log(f"Scene index: reusing {self.database_path} "
                 f"({self.count()} files, {age:.1f} days old)")
        return self.count()

    def count(self):
        return self.connection.execute("SELECT COUNT(*) FROM files").fetchone()[0]

    # -- download history --------------------------------------------------

    def record_download(self, path, scene_id, is_gallery, size, expected_bytes,
                        status, detail):
        """Note that a file was fetched, and how it fared its integrity check."""
        self.connection.execute(
            "INSERT OR REPLACE INTO downloads (path, scene_id, is_gallery, "
            "filename, bytes, expected_bytes, status, detail, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (path, int(scene_id) if scene_id else None, 1 if is_gallery else 0,
             os.path.basename(path), size, expected_bytes, status, detail,
             time.time()))
        self.connection.commit()

    def download_record(self, scene_id, is_gallery=False):
        """The most recent download attempt for a scene, if there was one."""
        return self.connection.execute(
            "SELECT * FROM downloads WHERE scene_id = ? AND is_gallery = ? "
            "ORDER BY downloaded_at DESC LIMIT 1",
            (int(scene_id), 1 if is_gallery else 0)).fetchone()

    def failed_downloads(self):
        """Everything that did not pass its check, for retry or inspection."""
        return self.connection.execute(
            "SELECT * FROM downloads WHERE status != 'ok' "
            "ORDER BY downloaded_at DESC").fetchall()

    def download_summary(self):
        """Counts per status, for the end-of-run report."""
        return {row['status']: row['n'] for row in self.connection.execute(
            "SELECT status, COUNT(*) AS n FROM downloads GROUP BY status")}

    # -- matching ---------------------------------------------------------

    def _dates_near(self, date):
        """The date plus/minus the allowed slack, as ISO strings."""
        if self.date_slack <= 0:
            return [date]
        from datetime import date as date_type, timedelta
        try:
            anchor = date_type.fromisoformat(date)
        except ValueError:
            return [date]
        return [(anchor + timedelta(days=offset)).isoformat()
                for offset in range(-self.date_slack, self.date_slack + 1)]

    def find(self, scene_id, date, title, gallery=False):
        """Return (matched_path, rule) if this scene is already on disk, else None.

        Matching is deliberately conservative: an id only counts when the release
        date agrees, which keeps a numeric id from some unrelated studio's naming
        scheme from masking an AdultTime clip that shares the number.
        """
        want_gallery = 1 if gallery else 0
        dates = self._dates_near(date)
        placeholders = ','.join('?' * len(dates))

        if scene_id:
            row = self.connection.execute(
                f"SELECT path FROM files WHERE scene_id = ? AND is_gallery = ? "
                f"AND date IN ({placeholders}) LIMIT 1",
                [int(scene_id), want_gallery, *dates]).fetchone()
            if row:
                return row['path'], 'id+date'

        title_norm = normalize(title)
        if title_norm:
            row = self.connection.execute(
                f"SELECT path FROM files WHERE is_gallery = ? "
                f"AND date IN ({placeholders}) "
                f"AND (title_norm = ? OR alt_norm = ?) LIMIT 1",
                [want_gallery, *dates, title_norm, title_norm]).fetchone()
            if row:
                return row['path'], 'date+title'

        return None

    def close(self):
        self.connection.close()
