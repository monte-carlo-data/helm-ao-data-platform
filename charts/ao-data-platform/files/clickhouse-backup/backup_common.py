"""Shared safe errors and API details for scheduled backups and previews."""

REQUEST_TIMEOUT_SECONDS = 30


class BackupError(Exception):
    """A safe-to-log failure message, without server bodies or credentials."""


def broken_local_entry(row):
    """Recognize the two incomplete-metadata descriptions emitted by v2.8.1."""
    desc = row.get("desc")
    return (row.get("location") == "local" and row.get("required") == ""
            and isinstance(desc, str)
            and (desc == "broken metadata.json not found" or desc.startswith("parse metadata.json error: ")))
