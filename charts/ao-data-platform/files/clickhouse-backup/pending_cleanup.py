"""Keep one unfinished S3 deletion across scheduler runs.

The stock API can keep deleting after an HTTP timeout. Persist the intent before
sending it and never resend it. Only its exact action on the same server instance
may overlap a later scheduled backup; all other cleanup waits for verification.
"""

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import re

from backup_common import BackupError, SCHEDULED_NAME


def fingerprint(row):
    return hashlib.sha256(json.dumps(row, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def valid_hashes(rows):
    return (isinstance(rows, dict)
            and all(isinstance(name, str) and name and isinstance(value, str)
                    and re.fullmatch(r"[a-f0-9]{64}", value) for name, value in rows.items()))


def valid_identity(identity):
    return (isinstance(identity, dict)
            and all(isinstance(identity.get(key), str) and identity[key]
                    for key in ("pod", "uid", "container_id", "password_revision"))
            and type(identity.get("restart_count")) is int and identity["restart_count"] >= 0)


class PendingRemoteDelete:
    def __init__(self, lease, apis, identity):
        self.lease, self.apis, self.identity = lease, tuple(apis), identity
        self.record = None
        self.refresh()

    def refresh(self):
        state, _ = self.lease.assert_owned()
        record = state.get("pending_delete")
        if record is not None:
            try:
                if (type(record["schema"]) is not int or record["schema"] != 1 or type(record["copy"]) is not int
                        or not 0 <= record["copy"] < len(self.apis)
                        or not SCHEDULED_NAME.fullmatch(record["name"])
                        or not valid_identity(record["identity"])
                        or not valid_hashes(record["remote"])
                        or record["name"] not in record["remote"]
                        or not isinstance(record["local"], list)
                        or len(record["local"]) != len(self.apis)
                        or any(not valid_hashes(rows) or record["name"] in rows for rows in record["local"])
                        or (record["action_start"] is not None
                            and (not isinstance(record["action_start"], str) or not record["action_start"]))
                        or not isinstance(record["created"], str)
                        or not isinstance(record["excluded"], list)
                        or record["name"] not in record["excluded"]
                        or any(not isinstance(name, str) or not SCHEDULED_NAME.fullmatch(name)
                               for name in record["excluded"])):
                    raise ValueError()
                if len(set(record["excluded"])) != len(record["excluded"]):
                    raise ValueError()
                created = datetime.fromisoformat(record["created"])
                if created.tzinfo is None or created.utcoffset().total_seconds() != 0:
                    raise ValueError()
            except (KeyError, TypeError, ValueError):
                raise BackupError("Saved S3 deletion is unreadable; no operation was sent.") from None
        self.record = deepcopy(record)
        return self.record

    def save(self, record):
        state, version = self.lease.assert_owned()
        if state.get("pending_delete") != self.record:
            raise BackupError("Saved S3 deletion changed; no further operation was sent.")
        state = deepcopy(state)
        if record is None:
            state.pop("pending_delete", None)
        else:
            state["pending_delete"] = deepcopy(record)
        self.lease.store.compare_and_swap(version, state)
        self.record = deepcopy(record)

    def prepare(self, index, name, remote, local, excluded):
        self.refresh()
        if self.record is not None:
            raise BackupError("An earlier S3 deletion still needs verification.")
        if (name not in excluded or name not in remote[1]
                or any(name in entries[0] for entries in local)):
            raise BackupError("S3 deletion requires an approved backup without local records.")
        command = "delete remote " + name
        rows = self.apis[index].request("GET", "/backup/actions")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BackupError("S3 action history is unreadable; no deletion was sent.")
        if any(row.get("command") == command for row in rows):
            raise BackupError("This S3 deletion already appears in history; it was not retried.")
        identity = self.identity(index)
        if not valid_identity(identity):
            raise BackupError("Could not identify the backup server; no deletion was sent.")
        record = {"schema": 1, "copy": index, "name": name,
                  "identity": identity, "action_start": None,
                  "created": datetime.now(timezone.utc).isoformat(),
                  "excluded": sorted(excluded),
                  "remote": {key: fingerprint(row) for key, row in remote[1].items()},
                  "local": [{key: fingerprint(row) for key, row in entries[1].items()}
                            for entries in local]}
        self.save(record)

    def before_send(self, index, name):
        self.refresh()
        if (self.record is None or (index, name) != (self.record["copy"], self.record["name"])
                or self.identity(index) != self.record["identity"]):
            raise BackupError("The S3 deletion target changed; no request was sent.")

    def not_sent(self):
        # Only a locally refused request or a proven connection failure reaches
        # here. A timeout or server error must keep the intent for inspection.
        self.save(None)

    def complete_verified(self):
        """Clear after this caller already verified success and both catalogs.

        RemoteCleaner performs the same strict full-list comparison as before.
        Do not repeat those network reads outside its reserved cleanup budget.
        """
        if self.record is None or self.identity(self.record["copy"]) != self.record["identity"]:
            raise BackupError("The backup server changed before deletion could be confirmed.")
        self.save(None)

    def observe(self):
        self.refresh()
        if self.record is None:
            return "none"
        index, name = self.record["copy"], self.record["name"]
        if self.identity(index) != self.record["identity"]:
            return "server_changed"
        rows = self.apis[index].request("GET", "/backup/actions")
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise BackupError("S3 action history is unreadable; deletion was not retried.")
        matches = [row for row in rows if row.get("command") == "delete remote " + name]
        if len(matches) != 1:
            return "unknown"
        row = matches[0]
        start = row.get("start")
        if not isinstance(start, str) or not start:
            return "unknown"
        if self.record["action_start"] is None:
            record = deepcopy(self.record)
            record["action_start"] = start
            self.save(record)
        elif self.record["action_start"] != start:
            return "unknown"
        status = row.get("status")
        return status if status in ("in progress", "success", "error", "cancel") else "unknown"

    def allows_action(self, index, row):
        """Only the registered old deletion may overlap a new scheduled backup."""
        if (self.record is None or index != self.record["copy"]
                or row.get("command") != "delete remote " + self.record["name"]
                or row.get("status") != "in progress"):
            return False
        return (self.observe() == "in progress"
                and row.get("start") == self.record["action_start"])

    def backup_catalog(self, rows):
        excluded = set(self.record["excluded"]) if self.record else set()
        # The scheduler only selects a current-day full backup. Explicitly
        # exclude every retiring backup as well, including its full base.
        return [row for row in rows if row.get("name") not in excluded]

    def confirm(self):
        """Require same-server success and unchanged retained records before clearing."""
        if self.observe() != "success":
            return False
        from backup_retention import validated_rows
        from cleanup_backups import plan as validate_remote
        name = self.record["name"]
        remote = [validated_rows(api.request("GET", "/backup/list/remote"), "remote")
                  for api in self.apis]
        if any(rows != remote[0] for rows in remote[1:]):
            raise BackupError("Copies disagree about S3 backups; deletion needs verification.")
        local = [validated_rows(api.request("GET", "/backup/list/local"), "local", remote[0][0])
                 for api in self.apis]
        validate_remote(remote[0][0], datetime.now(timezone.utc))
        if name in remote[0][0] or any(name in entries[0] for entries in local):
            raise BackupError("Deleted backup still appears in a list; deletion needs verification.")
        expected = dict(self.record["remote"])
        expected.pop(name, None)
        actual = {key: fingerprint(row) for key, row in remote[0][1].items()}
        if any(actual.get(key) != value for key, value in expected.items()):
            raise BackupError("An existing S3 backup changed during deletion; cleanup stopped.")
        for expected, entries in zip(self.record["local"], local):
            actual = {key: fingerprint(row) for key, row in entries[1].items()}
            if any(actual.get(key) != value for key, value in expected.items()):
                raise BackupError("Existing local backup records changed during S3 deletion; cleanup stopped.")
        # New successful backups and downloaded base records may have appeared
        # since the request, but an incomplete entry is not safe to clean past.
        if any(required is None for entries in local for required in entries[0].values()):
            raise BackupError("Incomplete local backup records need inspection before cleanup.")
        if self.observe() != "success":
            return False
        self.save(None)
        print("Backup cleanup: " + json.dumps({"event": "delete_verified", "location": "remote",
                                               "name": name}, sort_keys=True), flush=True)
        return True
