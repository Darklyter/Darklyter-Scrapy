"""Member-site credentials, read from an ini file so they stay out of the spiders.

Layout of ``tpdb/credentials.ini`` (gitignored)::

    [adulttime]
    username = someone
    password = secret

Spiders resolve credentials with :func:`resolve`, which lets a ``-a username=``
/ ``-a password=`` argument on the command line beat whatever the file says.
"""

import os
from configparser import ConfigParser

DEFAULT_CREDENTIALS_FILE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'credentials.ini')


class CredentialsError(RuntimeError):
    """Raised when a spider cannot work out what to log in with."""


def load_section(section, path=None):
    """Return the (username, password) stored for ``section``, or (None, None)."""
    path = path or DEFAULT_CREDENTIALS_FILE
    if not os.path.exists(path):
        return None, None
    parser = ConfigParser()
    parser.read(path)
    if not parser.has_section(section):
        return None, None
    return (parser.get(section, 'username', fallback=None),
            parser.get(section, 'password', fallback=None))


def resolve(section, username=None, password=None, path=None):
    """Work out the credentials to use, preferring explicit arguments.

    ``username``/``password`` come from the spider's ``-a`` arguments; anything
    left unset falls back to the ini file.  Raises :class:`CredentialsError`
    when the pair cannot be completed, so the spider fails at start-up with a
    useful message rather than part-way through a crawl with a 403.
    """
    file_username, file_password = load_section(section, path)
    username = username or file_username
    password = password or file_password

    if not username or not password:
        raise CredentialsError(
            f"No credentials for '{section}'. Either pass "
            f"-a username=... -a password=... or add:\n\n"
            f"    [{section}]\n"
            f"    username = ...\n"
            f"    password = ...\n\n"
            f"to {path or DEFAULT_CREDENTIALS_FILE}")

    return username, password
