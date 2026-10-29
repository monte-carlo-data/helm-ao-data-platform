"""Shared safe errors and API details for scheduled backups and previews."""

import re
import uuid


REQUEST_TIMEOUT_SECONDS = 30
NAME_PREFIX = "ao-otel-"
SCHEDULED_NAME = re.compile(re.escape(NAME_PREFIX) + r"(full|incremental)-(\d{8}T\d{6}Z)-[0-9a-f]{8}\Z")
DELETION_UNAVAILABLE = "S3 deletion requires daily-weekly retention; count-and-age supports preview only."


class BackupError(Exception):
    """A safe-to-log failure message, without server bodies or credentials."""


class RequestNotSent(BackupError):
    """The request was refused locally or could not establish a connection."""


class RevisionError(BackupError):
    """A password rotation stops the Job before sending another request."""


class RevisionCheckUnavailable(BackupError):
    """Credentials must not be sent before the revision can be checked."""


class CleanupStopped(BackupError):
    """Preserve verified removals and any deletion still needing confirmation."""

    def __init__(self, message, result):
        super().__init__(message)
        self.result = result


def scheduled_name(kind, now):
    """Use the same name format for creation, full-base selection, and cleanup."""
    if kind not in ("full", "incremental"):
        raise BackupError("A scheduled backup must be full or incremental.")
    return f"{NAME_PREFIX}{kind}-{now:%Y%m%dT%H%M%SZ}-{uuid.uuid4().hex[:8]}"


def broken_local_entry(row):
    """Recognize the two incomplete-metadata descriptions emitted by v2.8.1."""
    desc = row.get("desc")
    return (row.get("location") == "local" and row.get("required") == ""
            and isinstance(desc, str)
            and (desc == "broken metadata.json not found" or desc.startswith("parse metadata.json error: ")))
