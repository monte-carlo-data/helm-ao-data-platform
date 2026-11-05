"""Exercise cleanup state and competing Jobs without a Kubernetes connection."""

import copy
import io
import json
from pathlib import Path
import sys
import unittest
from unittest import mock
import urllib.error

DIRECTORY = Path(__file__).resolve().parents[2] / "charts/ao-data-platform/files/clickhouse-backup"
sys.path.insert(0, str(DIRECTORY))
import cleanup_store as store_module
sys.path.pop(0)


def resource(state=None, version="1"):
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {
        "name": "backup-state", "namespace": "test-space", "uid": "state-uid",
        "resourceVersion": version, "labels": {"keep": "yes"}},
        "data": {"state.json": json.dumps({} if state is None else state), "other": "preserve"},
        "binaryData": {"other": "eA=="}}


class Response:
    status = 200

    def __init__(self, value, status=200):
        self.status = status
        self.body = io.BytesIO(value if isinstance(value, bytes) else json.dumps(value).encode())

    def read(self, count):
        return self.body.read(count)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.body.close()


class Kubernetes:
    def __init__(self, state=None):
        self.resource = resource(state)
        self.calls = []
        self.failure = None
        self.reply = None
        self.before_put = None

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if self.failure is not None:
            raise self.failure
        if self.reply is not None:
            return self.reply()
        if request.get_method() == "PUT":
            if self.before_put:
                self.before_put()
            body = json.loads(request.data)
            if body["metadata"]["resourceVersion"] != self.resource["metadata"]["resourceVersion"]:
                raise urllib.error.HTTPError(request.full_url, 409, "private response", {}, io.BytesIO())
            body["metadata"]["resourceVersion"] = str(int(body["metadata"]["resourceVersion"]) + 1)
            self.resource = body
        return Response(copy.deepcopy(self.resource))


class StoreFixture(unittest.TestCase):
    def setUp(self):
        self.token = mock.patch.object(Path, "read_text", return_value="test-token\n")
        self.read_token = self.token.start()
        self.addCleanup(self.token.stop)
        self.server = Kubernetes({"pending": {"name": "old-backup"}})
        self.store = self.new_store()

    def new_store(self):
        return store_module.KubernetesCleanupStore("test-space", "backup-state", opener=self.server)


class StoreTests(StoreFixture):
    def test_load_and_update_preserve_other_state_and_configmap_fields(self):
        state, version = self.store.load()
        state["run"] = {"owner": "job-one", "expires": 100}
        self.assertEqual(self.store.compare_and_swap(version, state), "2")
        saved, version = self.store.load()
        self.assertEqual(saved, state)
        self.assertEqual(saved["pending"], {"name": "old-backup"})
        self.assertEqual(self.server.resource["data"]["other"], "preserve")
        self.assertEqual(self.server.resource["metadata"]["labels"], {"keep": "yes"})
        self.assertEqual(self.server.resource["binaryData"], {"other": "eA=="})

    def test_absent_state_is_empty_without_changing_other_data(self):
        del self.server.resource["data"]["state.json"]
        state, version = self.store.load()
        self.assertEqual(state, {})
        self.store.compare_and_swap(version, {"pending": {}})
        self.assertEqual(self.server.resource["data"]["other"], "preserve")

    def test_missing_data_is_empty(self):
        del self.server.resource["data"]
        state, version = self.store.load()
        self.assertEqual(state, {})
        self.store.compare_and_swap(version, {"pending": {}})

    def test_unchanged_put_can_confirm_the_same_resource_version(self):
        self.server.resource["data"]["state.json"] = store_module.encode_state({"pending": {}})
        state, version = self.store.load()
        self.server.reply = lambda: Response(copy.deepcopy(self.server.resource))
        self.assertEqual(self.store.compare_and_swap(version, state), version)

    def test_competing_writer_gets_conflict_without_retry_or_overwrite(self):
        other = self.new_store()
        first, version = self.store.load()
        second, other_version = other.load()
        first["run"] = {"owner": "one", "expires": 100}
        self.store.compare_and_swap(version, first)
        second["run"] = {"owner": "two", "expires": 100}
        with self.assertRaises(store_module.CleanupStateConflict):
            other.compare_and_swap(other_version, second)
        self.assertEqual(len(self.server.calls), 4)
        self.assertEqual(json.loads(self.server.resource["data"]["state.json"]), first)

    def test_update_requires_matching_loaded_version(self):
        for version in ("1", "", None):
            with self.assertRaises(store_module.CleanupStateConflict):
                self.store.compare_and_swap(version, {})
        self.store.load()
        with self.assertRaises(store_module.CleanupStateConflict):
            self.store.compare_and_swap("different", {})
        self.assertEqual(len(self.server.calls), 1)

    def test_token_is_read_for_every_request_and_requests_are_bounded(self):
        self.read_token.side_effect = ["first-token", "second-token"]
        state, version = self.store.load()
        self.store.compare_and_swap(version, state)
        self.assertEqual([request.get_header("Authorization") for request, _ in self.server.calls],
                         ["Bearer first-token", "Bearer second-token"])
        self.assertEqual([timeout for _, timeout in self.server.calls], [10, 10])
        self.assertEqual([request.get_method() for request, _ in self.server.calls], ["GET", "PUT"])

    def test_url_uses_fixed_host_and_quotes_names(self):
        store = store_module.KubernetesCleanupStore("test/space", "state?extra=x", opener=self.server)
        self.assertEqual(store.url, "https://kubernetes.default.svc/api/v1/namespaces/"
                         "test%2Fspace/configmaps/state%3Fextra%3Dx")
        for method in ("POST", "DELETE", "PATCH"):
            with self.assertRaises(store_module.BackupError):
                store.request(method)
        self.assertEqual(self.server.calls, [])

    def test_default_opener_verifies_tls_and_refuses_redirects(self):
        with mock.patch.object(store_module.ssl, "create_default_context") as tls, \
                mock.patch.object(store_module.urllib.request, "build_opener") as opener:
            store_module.KubernetesCleanupStore("test-space", "backup-state")
        tls.assert_called_once_with(cafile="/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")
        self.assertIsInstance(opener.call_args.args[0], store_module.NoRedirect)
        self.assertIsNone(store_module.NoRedirect().redirect_request(None, None, 302, None, {}, "https://other"))

    def test_get_pod_uses_named_get_and_validates_identity(self):
        pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
            "name": "backup-job-one", "namespace": "test-space", "uid": "job-pod-uid"},
            "status": {"phase": "Succeeded"}}
        self.server.reply = lambda: Response(pod)
        self.assertEqual(self.store.get_pod("backup-job-one"), pod)
        request, timeout = self.server.calls[-1]
        self.assertEqual(request.full_url, "https://kubernetes.default.svc/api/v1/namespaces/"
                         "test-space/pods/backup-job-one")
        self.assertEqual((request.get_method(), timeout), ("GET", 10))
        pod["metadata"]["uid"] = ""
        with self.assertRaises(store_module.BackupError):
            self.store.get_pod("backup-job-one")

    def test_only_missing_pod_returns_none(self):
        self.server.failure = urllib.error.HTTPError(self.store.url, 404, "private-body", {}, io.BytesIO())
        self.assertIsNone(self.store.get_pod("backup-job-one"))
        with self.assertRaises(store_module.BackupError):
            self.store.load()
        self.server.failure = urllib.error.HTTPError(self.store.url, 403, "private-body", {}, io.BytesIO())
        with self.assertRaises(store_module.BackupError) as error:
            self.store.get_pod("backup-job-one")
        self.assertNotIn("private-body", str(error.exception))

    def test_timeout_and_http_errors_do_not_leak_or_retry(self):
        for failure in (TimeoutError("private-token"), urllib.error.URLError("private-token"),
                        urllib.error.HTTPError(self.store.url, 403, "private-token", {}, io.BytesIO())):
            with self.subTest(failure=type(failure).__name__):
                self.server.failure = failure
                before = len(self.server.calls)
                with self.assertRaises(store_module.BackupError) as error:
                    self.store.load()
                self.assertNotIn("private-token", str(error.exception))
                self.assertEqual(len(self.server.calls), before + 1)

    def test_lost_put_response_is_not_retried(self):
        state, version = self.store.load()
        self.server.failure = TimeoutError("private-token")
        with self.assertRaises(store_module.BackupError):
            self.store.compare_and_swap(version, state)
        self.assertEqual(len(self.server.calls), 2)

    def test_unexpected_resource_or_response_is_rejected(self):
        values = [[], {"kind": "Status", "message": "private-body"}, resource()]
        values[-1]["metadata"]["name"] = "other"
        for field, value in (("uid", ""), ("resourceVersion", None), ("namespace", "other"),
                             ("deletionTimestamp", "2026-01-01T00:00:00Z")):
            item = resource()
            item["metadata"][field] = value
            values.append(item)
        for value in values:
            with self.subTest(value=value):
                self.server.reply = lambda value=value: Response(value)
                with self.assertRaises(store_module.BackupError) as error:
                    self.store.load()
                self.assertNotIn("private-body", str(error.exception))
        for response in (lambda: Response(b"not JSON"), lambda: Response(resource(), status=201),
                         lambda: Response(b"x" * (store_module.MAX_RESPONSE_BYTES + 1))):
            self.server.reply = response
            with self.assertRaises(store_module.BackupError):
                self.store.load()

    def test_recreated_configmap_cannot_replace_loaded_object(self):
        self.store.load()
        self.server.resource["metadata"]["uid"] = "replacement-uid"
        with self.assertRaises(store_module.BackupError):
            self.store.load()

    def test_put_response_must_keep_uid_and_confirm_state_and_new_version(self):
        for change in ("uid", "state", "version"):
            with self.subTest(change=change):
                self.server.reply = None
                self.store = self.new_store()
                state, version = self.store.load()
                reply = resource(state, version="2")
                if change == "uid":
                    reply["metadata"]["uid"] = "replacement-uid"
                elif change == "state":
                    reply["data"]["state.json"] = "{}"
                else:
                    reply["metadata"]["resourceVersion"] = version
                self.server.reply = lambda: Response(reply)
                with self.assertRaises(store_module.BackupError):
                    self.store.compare_and_swap(version, state)

    def test_invalid_and_oversized_state_never_gets_written(self):
        self.store.load()
        for value in ([], {"bad": float("nan")}, {"large": "x" * store_module.MAX_STATE_BYTES},
                      {"bad": object()}):
            with self.assertRaises(store_module.BackupError):
                self.store.compare_and_swap("1", value)
        self.assertEqual(len(self.server.calls), 1)
        for encoded in ("[]", "broken", "null", '{"bad": NaN}', "x" * (store_module.MAX_STATE_BYTES + 1)):
            self.server.resource["data"]["state.json"] = encoded
            with self.assertRaises(store_module.BackupError):
                self.store.load()


class RunLeaseTests(StoreFixture):
    def setUp(self):
        super().setUp()
        self.seconds = 100
        self.lease = store_module.CleanupRunLease(self.store, "job-one", now=lambda: self.seconds, duration=60)

    def test_acquire_assert_and_release_preserve_pending_and_later_updates(self):
        state, version = self.lease.acquire()
        self.assertEqual(state["run"], {"owner": "job-one", "expires": 160})
        self.assertEqual(self.lease.assert_owned(), (state, version))
        state["pending"]["phase"] = "running"
        self.store.compare_and_swap(version, state)
        self.assertTrue(self.lease.release())
        self.assertEqual(self.store.load()[0], {"pending": {"name": "old-backup", "phase": "running"}})
        self.assertFalse(self.lease.release())

    def test_same_owner_can_check_acquisition_without_extending_expiry(self):
        initial = self.lease.acquire()
        self.seconds += 20
        self.assertEqual(self.lease.acquire(), initial)

    def test_unexpired_owner_cannot_be_replaced_without_finished_proof(self):
        self.lease.acquire()
        finished = mock.Mock(return_value=False)
        other = store_module.CleanupRunLease(self.new_store(), "job-two", now=lambda: self.seconds,
                                             owner_finished=finished)
        with self.assertRaises(store_module.CleanupStateConflict):
            other.acquire()
        finished.assert_called_once_with("job-one")

    def test_confirmed_dead_owner_can_be_replaced_before_expiry(self):
        self.lease.acquire()
        other = store_module.CleanupRunLease(self.new_store(), "job-two", now=lambda: self.seconds,
                                             duration=30, owner_finished=lambda owner: owner == "job-one")
        state, _ = other.acquire()
        self.assertEqual(state["run"], {"owner": "job-two", "expires": 130})

    def test_expired_unknown_owner_remains_blocked(self):
        self.lease.acquire()
        self.seconds = 161
        for callback in (None, lambda owner: False, lambda owner: 1):
            other = store_module.CleanupRunLease(self.new_store(), "job-two", now=lambda: self.seconds,
                                                 owner_finished=callback)
            with self.assertRaises(store_module.CleanupStateConflict):
                other.acquire()
        with self.assertRaises(store_module.CleanupStateConflict):
            self.lease.assert_owned()
        with self.assertRaises(store_module.CleanupStateConflict):
            self.lease.acquire()

    def test_confirmed_finished_expired_owner_can_be_replaced(self):
        self.lease.acquire()
        self.seconds = 161
        finished = mock.Mock(return_value=True)
        other = store_module.CleanupRunLease(self.new_store(), "job-two", now=lambda: self.seconds,
                                             duration=30, owner_finished=finished)
        state, _ = other.acquire()
        finished.assert_called_once_with("job-one")
        self.assertEqual(state["run"], {"owner": "job-two", "expires": 191})
        self.assertFalse(self.lease.release())
        with self.assertRaises(store_module.CleanupStateConflict):
            self.lease.assert_owned()

    def test_callback_failure_is_generic_and_does_not_replace_owner(self):
        self.lease.acquire()
        self.seconds = 161
        other = store_module.CleanupRunLease(self.new_store(), "job-two", now=lambda: self.seconds,
                                             owner_finished=mock.Mock(side_effect=RuntimeError("private-token")))
        with self.assertRaises(store_module.BackupError) as error:
            other.acquire()
        self.assertNotIn("private-token", str(error.exception))
        self.assertEqual(self.store.load()[0]["run"]["owner"], "job-one")

    def test_release_loses_race_without_overwriting_new_pending_state(self):
        self.lease.acquire()

        def changed():
            state = json.loads(self.server.resource["data"]["state.json"])
            state["pending"]["phase"] = "complete"
            self.server.resource["data"]["state.json"] = json.dumps(state)
            self.server.resource["metadata"]["resourceVersion"] = "3"

        self.server.before_put = changed
        with self.assertRaises(store_module.CleanupStateConflict):
            self.lease.release()
        self.assertEqual(self.store.load()[0]["pending"]["phase"], "complete")

    def test_acquire_loses_race_without_overwriting_other_owner(self):
        def changed():
            state = json.loads(self.server.resource["data"]["state.json"])
            state["run"] = {"owner": "job-two", "expires": 200}
            self.server.resource["data"]["state.json"] = json.dumps(state)
            self.server.resource["metadata"]["resourceVersion"] = "2"

        self.server.before_put = changed
        with self.assertRaises(store_module.CleanupStateConflict):
            self.lease.acquire()
        self.assertEqual(self.store.load()[0]["run"]["owner"], "job-two")

    def test_malformed_saved_owner_does_not_grant_permission(self):
        for run in (None, {}, {"owner": "job-one", "expires": True},
                    {"owner": "job-one", "expires": "later"}):
            self.server.resource["data"]["state.json"] = json.dumps({"run": run})
            with self.assertRaises(store_module.BackupError):
                self.lease.acquire()
            with self.assertRaises(store_module.BackupError):
                self.lease.assert_owned()


if __name__ == "__main__":
    unittest.main()
