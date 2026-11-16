"""Shared safe errors and API details for scheduled backups and previews."""

import re
import uuid


REQUEST_TIMEOUT_SECONDS = 30
DELETE_TIMEOUT_SECONDS = 300
MAX_REMOTE_DELETE_TIMEOUT_SECONDS = 3600
# Healthy remote desc is "<stored format>, embedded"; compression_format: none
# appears as directory. Keep tar aligned with the config and environment setting
# in templates/_backup.tpl, and directory while pre-6.2.0 backups can remain.
REMOTE_EMBEDDED_DESCRIPTIONS = ("directory, embedded", "tar, embedded")
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


def validate_delete_timeout(seconds):
    if type(seconds) is not int or not DELETE_TIMEOUT_SECONDS <= seconds <= MAX_REMOTE_DELETE_TIMEOUT_SECONDS:
        raise BackupError("S3 delete timeout must be an integer from 300 to 3600 seconds.")
    return seconds
