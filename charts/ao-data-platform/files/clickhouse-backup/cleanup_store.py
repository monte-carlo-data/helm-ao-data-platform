"""Save cleanup progress in one existing Kubernetes ConfigMap.

Updates use resourceVersion so concurrent Jobs cannot overwrite each other's
progress. A lost write response is never retried here. The caller must read the
saved state before deciding what to do next.
"""

import copy
import http.client
import json
import math
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from backup_common import BackupError


MAX_STATE_BYTES = 512 * 1024
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
REQUEST_TIMEOUT_SECONDS = 10
SERVICE_ACCOUNT_DIRECTORY = Path("/var/run/secrets/kubernetes.io/serviceaccount")


class CleanupStateConflict(BackupError):
    """Another writer changed cleanup state or still owns the scheduled run."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def encode_state(state):
    if not isinstance(state, dict):
        raise BackupError("Cleanup state must be a JSON object.")
    try:
        encoded = json.dumps(state, sort_keys=True, separators=(",", ":"), allow_nan=False)
        if len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("oversized state")
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise BackupError("Cleanup state is invalid or exceeds its size limit.") from None
    return encoded


def decode_state(encoded):
    try:
        if not isinstance(encoded, str) or len(encoded.encode("utf-8")) > MAX_STATE_BYTES:
            raise ValueError("invalid state size")
        state = json.loads(encoded)
        encode_state(state)
        return state
    except (TypeError, ValueError, OverflowError, RecursionError):
        raise BackupError("Saved cleanup state is unreadable.") from None


class KubernetesCleanupStore:
    def __init__(self, namespace, name, opener=None):
        if any(not isinstance(value, str) or not value or value in (".", "..")
               for value in (namespace, name)):
            raise BackupError("Cleanup state requires a namespace and ConfigMap name.")
        self.namespace, self.name = namespace, name
        self.url = ("https://kubernetes.default.svc/api/v1/namespaces/"
                    + urllib.parse.quote(namespace, safe="") + "/configmaps/"
                    + urllib.parse.quote(name, safe=""))
        self.uid = None
        self.resource = None
        if opener is None:
            try:
                context = ssl.create_default_context(cafile=str(SERVICE_ACCOUNT_DIRECTORY / "ca.crt"))
                opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))
            except (OSError, ValueError):
                raise BackupError("Could not load Kubernetes credentials for cleanup state.") from None
        self.opener = opener

    def request(self, method, resource=None, *, pod_name=None):
        if (method not in ("GET", "PUT") or (method == "PUT" and not isinstance(resource, dict))
                or (method == "GET" and resource is not None)):
            raise BackupError("Cleanup state refused an unexpected Kubernetes request.")
        url = self.url
        if pod_name is not None:
            if method != "GET" or not isinstance(pod_name, str) or not pod_name or pod_name in (".", ".."):
                raise BackupError("Cleanup run requires a valid Pod name.")
            url = ("https://kubernetes.default.svc/api/v1/namespaces/"
                   + urllib.parse.quote(self.namespace, safe="") + "/pods/"
                   + urllib.parse.quote(pod_name, safe=""))
        try:
            # Projected service account tokens rotate while a Job is running.
            token = (SERVICE_ACCOUNT_DIRECTORY / "token").read_text().strip()
            if not token or any(character.isspace() for character in token):
                raise ValueError("invalid token")
            data = None if resource is None else json.dumps(resource, allow_nan=False).encode("utf-8")
            if data is not None and len(data) > MAX_RESPONSE_BYTES:
                raise ValueError("oversized ConfigMap")
            request = urllib.request.Request(
                url, method=method, data=data,
                headers={"Authorization": "Bearer " + token, "Accept": "application/json",
                         "Content-Type": "application/json"})
            with self.opener.open(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise ValueError("unexpected response status")
                payload = response.read(MAX_RESPONSE_BYTES + 1)
            if len(payload) > MAX_RESPONSE_BYTES:
                raise ValueError("oversized response")
            return json.loads(payload)
        except urllib.error.HTTPError as error:
            code = error.code
            error.close()
            if code == 404 and pod_name is not None:
                return None
            if code == 409:
                raise CleanupStateConflict("Cleanup state changed; no update was retried.") from None
            raise BackupError("Kubernetes rejected the cleanup state request; no retry was sent.") from None
        except (OSError, ValueError, TypeError, OverflowError, RecursionError,
                urllib.error.URLError, http.client.HTTPException):
            raise BackupError("Cleanup state request could not be confirmed; no retry was sent.") from None

    def get_pod(self, name):
        if not isinstance(name, str) or not name or name in (".", ".."):
            raise BackupError("Cleanup run requires a valid Pod name.")
        resource = self.request("GET", pod_name=name)
        if resource is None:
            return None
        metadata = resource.get("metadata") if isinstance(resource, dict) else None
        if (not isinstance(resource, dict) or resource.get("apiVersion") != "v1"
                or resource.get("kind") != "Pod" or not isinstance(metadata, dict)
                or metadata.get("name") != name or metadata.get("namespace") != self.namespace
                or not isinstance(metadata.get("uid"), str) or not metadata["uid"]):
            raise BackupError("Kubernetes returned an unexpected cleanup run Pod response.")
        return resource

    def validate_resource(self, resource):
        if not isinstance(resource, dict):
            raise BackupError("Kubernetes returned an unexpected cleanup state response.")
        metadata = resource.get("metadata")
        if (resource.get("apiVersion") != "v1" or resource.get("kind") != "ConfigMap"
                or not isinstance(metadata, dict)
                or metadata.get("name") != self.name or metadata.get("namespace") != self.namespace
                or not isinstance(metadata.get("uid"), str) or not metadata["uid"]
                or not isinstance(metadata.get("resourceVersion"), str) or not metadata["resourceVersion"]
                or metadata.get("deletionTimestamp") is not None
                or (self.uid is not None and metadata["uid"] != self.uid)):
            raise BackupError("Cleanup state ConfigMap is missing, replaced, or unreadable.")
        data = resource.get("data", {})
        if not isinstance(data, dict):
            raise BackupError("Cleanup state ConfigMap data is unreadable.")
        state = decode_state(data["state.json"]) if "state.json" in data else {}
        return state, metadata["resourceVersion"]

    def load(self):
        resource = self.request("GET")
        state, version = self.validate_resource(resource)
        self.uid = resource["metadata"]["uid"]
        self.resource = copy.deepcopy(resource)
        return state, version

    def compare_and_swap(self, version, state):
        encoded = encode_state(state)
        if (not isinstance(version, str) or not version or self.resource is None
                or self.resource["metadata"]["resourceVersion"] != version):
            raise CleanupStateConflict("Cleanup state must be read before updating this version.")
        resource = copy.deepcopy(self.resource)
        resource.setdefault("data", {})["state.json"] = encoded
        updated = self.request("PUT", resource)
        actual, new_version = self.validate_resource(updated)
        if actual != decode_state(encoded) or (new_version == version and resource != self.resource):
            raise BackupError("Cleanup state update returned an unexpected result; no retry was sent.")
        self.resource = copy.deepcopy(updated)
        return new_version


class CleanupRunLease:
    """Allow one Job to send backup requests while preserving pending cleanup.

    Expiry does not prove that a previous Job stopped. Taking its place requires
    a caller-supplied check that confirms the previous owner finished. Confirmed
    termination permits recovery immediately, even before the recorded expiry.
    Call assert_owned immediately before each mutation; it does not renew expiry.
    """

    def __init__(self, store, owner, now=time.time, duration=14400, owner_finished=None):
        if (not isinstance(owner, str) or not owner or type(duration) not in (int, float)
                or not math.isfinite(duration) or duration <= 0
                or (owner_finished is not None and not callable(owner_finished))):
            raise BackupError("Cleanup run ownership settings are invalid.")
        self.store, self.owner, self.now = store, owner, now
        self.duration, self.owner_finished = duration, owner_finished

    def current_time(self):
        value = self.now()
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise BackupError("Cleanup run ownership requires a valid current time.")
        return value

    @staticmethod
    def run_record(state):
        if "run" not in state:
            return None
        run = state["run"]
        if (not isinstance(run, dict) or not isinstance(run.get("owner"), str) or not run["owner"]
                or type(run.get("expires")) not in (int, float)
                or not math.isfinite(run["expires"]) or run["expires"] < 0):
            raise BackupError("Saved cleanup run ownership is unreadable.")
        return run

    def acquire(self):
        state, version = self.store.load()
        run, now = self.run_record(state), self.current_time()
        if run is not None:
            if run["owner"] == self.owner and run["expires"] > now:
                return state, version
            finished = False
            if run["owner"] != self.owner and self.owner_finished is not None:
                try:
                    finished = self.owner_finished(run["owner"]) is True
                except Exception:
                    raise BackupError("Could not confirm that the previous cleanup run finished.") from None
            if not finished:
                raise CleanupStateConflict("Another cleanup run owns the schedule or has not been confirmed finished.")
        state["run"] = {"owner": self.owner, "expires": now + self.duration}
        version = self.store.compare_and_swap(version, state)
        return state, version

    def assert_owned(self):
        state, version = self.store.load()
        run = self.run_record(state)
        if run is None or run["owner"] != self.owner or run["expires"] <= self.current_time():
            raise CleanupStateConflict("Cleanup run ownership changed or expired; no further request is allowed.")
        return state, version

    def release(self):
        state, version = self.store.load()
        run = self.run_record(state)
        if run is None or run["owner"] != self.owner:
            return False
        del state["run"]
        self.store.compare_and_swap(version, state)
        return True
