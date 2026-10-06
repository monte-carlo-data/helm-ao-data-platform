#!/usr/bin/env python3
"""Prepare one controlled backup test; --run explicitly creates its Kubernetes Job.

The installed scheduler gets an unreachable first endpoint for this Job only.
Neither ClickHouse copy is stopped. No password is read outside the test Pod.
A timeout cannot stop a server operation: inspect the result before doing anything else.
"""

import argparse
import copy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time


POD_SCRIPT = r'''
import contextlib, hashlib, io, json, os, sys
from datetime import datetime, timezone
from pathlib import Path
sys.path.insert(0, "/scripts")
import run_backup as b

class CheckFailed(Exception): pass
def check(ok, message):
    if not ok: raise CheckFailed(message)
def window():
    now = datetime.now(timezone.utc)
    seconds = now.hour % 4 * 3600 + now.minute * 60 + now.second
    check(seconds > 900 and 14400 - seconds > timeout + 900,
          "Too close to a scheduled run; no further request was sent.")
    return now
def catalog(api, where):
    rows = api.request("GET", "/backup/list/" + where)
    check(len({r["name"] for r in rows}) == len(rows), "Duplicate catalog names.")
    return {r["name"]: r for r in rows}
def actions(api):
    # v2.8.1 /backup/status shows only the last action; /actions shows all history.
    rows = api.request("GET", "/backup/actions")
    check(all(isinstance(r.get("command"), str) and r.get("status") in
              ("success", "error", "cancel", "in progress") for r in rows), "Unexpected action history.")
    check(not any(r.get("status") == "in progress" for r in rows),
          "A backup command is already running; stop and inspect it.")
    mutations = [r for r in rows if r["command"].split(" ", 1)[0] not in ("list", "tables")]
    check(all(isinstance(r.get("operation_id"), str) and r["operation_id"] for r in mutations),
          "An operation ID is missing; action history cannot be verified.")
    return sorted((r["operation_id"], r["command"], r.get("start", ""), r["status"]) for r in mutations)

sent = []
try:
    expected = os.environ["VERIFY_EXPECTED_FULL"]
    timeout = int(os.environ["VERIFY_TIMEOUT_SECONDS"])
    check(hashlib.sha256(Path("/scripts/run_backup.py").read_bytes()).hexdigest()
          == os.environ["VERIFY_SCRIPT_SHA256"], "The installed scheduler changed after preparation.")
    now = window()
    endpoints = json.loads(os.environ["BACKUP_ENDPOINTS"])
    check(len(endpoints) == 2 and endpoints[0] != endpoints[1], "Two distinct backup endpoints are required.")
    real, probes = b.configured_clients()
    for api in real: actions(api)
    before = catalog(real[0], "remote")
    check(catalog(real[1], "remote") == before, "The copies disagree about remote backups.")
    check(b.full_base(list(before.values()), now) == expected,
          "The expected full backup is not today's latest healthy full backup.")
    local0 = catalog(real[0], "local")
    check(local0.get(expected, {}).get("desc") == "embedded" and local0[expected].get("required") == "",
          "The expected full backup must already exist on copy 0.")
    check(expected not in catalog(real[1], "local"),
          "Copy 1 already has this full backup; use a future full to test downloading it.")
    baseline0 = actions(real[0])
    check(bool(baseline0), "Copy 0's operation history is empty; this check would be inconclusive.")
    actions(real[1])
    for api in real:
        check(catalog(api, "remote") == before, "The remote catalog changed during preflight.")
    selected = []
    class UnreachableCopy:
        def request(self, *args, **kwargs):
            raise b.RequestNotSent("Copy 0 is unavailable for this controlled test; no request was sent.")
    scheduler = b.Scheduler([UnreachableCopy(), real[1]], probes,
                            timeout=timeout - 30, poll_seconds=5, **b.configured_options())
    choose = scheduler.choose_copy
    def choose_copy(backup_time=None):
        result = choose(backup_time)
        check(result[0] == 1, "The scheduler did not select copy 1.")
        check(result[2] == expected, "The scheduler selected a different full backup.")
        selected.append(1)
        return result
    scheduler.choose_copy = choose_copy
    request = real[1].request
    def guarded_request(method, path, query=None):
        if method == "POST":
            window()
            check(selected == [1], "The scheduler must select copy 1 exactly once.")
            check(actions(real[0]) == baseline0, "Copy 0's action history changed.")
            actions(real[1])
            for api in real:
                check(catalog(api, "remote") == before, "Another backup changed the remote catalog.")
            operation = "download" if path == "/backup/download/" + expected else "create_remote"
            check((not sent and operation == "download") or
                  (sent == ["download"] and path == "/backup/create_remote"
                   and query.get("diff-from-remote") == expected and query.get("table") == b.TABLES),
                  "Unexpected backup request; no further request was sent.")
            sent.append(operation)
        return request(method, path, query)
    real[1].request = guarded_request
    with contextlib.redirect_stdout(io.StringIO()):
        name = scheduler.run(now)
    after = catalog(real[1], "remote")
    check(catalog(real[0], "remote") == after, "The copies disagree after the backup.")
    check(set(after) - set(before) == {name} and all(after.get(k) == v for k, v in before.items()),
          "The remote catalog did not gain exactly one backup.")
    # The exact healthy description excludes broken-backup errors. "directory"
    # requires s3.compression_format: none in templates/_backup.tpl; keep this
    # check and run_backup.py's catalog checks in sync with that setting.
    check(after[name].get("required") == expected and after[name].get("desc") == "directory, embedded"
          and after[name].get("location") == "remote" and "-incremental-" in name,
          "The new backup is not a healthy incremental using the expected full.")
    local1 = catalog(real[1], "local")
    check(local1.get(expected, {}).get("desc") == "embedded" and local1[expected].get("required") == ""
          and local1.get(name, {}).get("desc") == "embedded" and local1[name].get("required") == expected,
          "Copy 1's downloaded full metadata or new backup is missing.")
    check(name not in catalog(real[0], "local") and actions(real[0]) == baseline0,
          "Copy 0 created work or its action history changed.")
    actions(real[1])
    check(sent == ["download", "create_remote"], "The test did not submit exactly the expected two operations.")
    print("EVIDENCE " + json.dumps({"status":"passed", "selected_copy":1,
          "backup":name, "type":"incremental", "required":expected,
          "remote_before":len(before), "remote_after":len(after),
          "downloaded_full_metadata":True, "copy0_mutations":0}, sort_keys=True))
except Exception as error:
    reason = str(error) if isinstance(error, (CheckFailed, b.BackupError)) else "Verification failed; inspect the Job before continuing."
    print("EVIDENCE " + json.dumps({"status":"inconclusive", "reason":reason,
          "requests_sent":len(sent), "instruction":"Stop and inspect; do not rerun automatically."}, sort_keys=True))
    sys.exit(1)
'''


class CheckFailed(Exception):
    pass


def kubectl(args, *command):
    result = subprocess.run(["kubectl", "--context", args.context, "--namespace", args.namespace,
                             *command], capture_output=True, text=True, timeout=40)
    if result.returncode:
        raise CheckFailed("kubectl failed. Stop and inspect the named Job; do not retry creating it.")
    return result.stdout


def prepare(args, now):
    cron = json.loads(kubectl(args, "get", "cronjob", args.cronjob, "-o", "json"))
    spec = cron["spec"]
    if (spec["schedule"] != "0 */4 * * *" or spec.get("timeZone") not in ("Etc/UTC", "UTC")
            or spec.get("suspend", False) or spec.get("concurrencyPolicy") != "Forbid"
            or cron.get("status", {}).get("active")):
        raise CheckFailed("Expected an idle, unsuspended four-hour UTC CronJob with concurrencyPolicy Forbid.")
    seconds = now.hour % 4 * 3600 + now.minute * 60 + now.second
    if seconds <= 900 or 14400 - seconds <= args.timeout_seconds + 900:
        raise CheckFailed("Choose a time at least 15 minutes after the last run and 15 minutes before the next, including the test timeout.")
    jobs = json.loads(kubectl(args, "get", "jobs", "-o", "json"))["items"]
    labels = spec["jobTemplate"]["spec"]["template"]["metadata"]["labels"]
    for job in jobs:
        metadata, status = job["metadata"], job.get("status", {})
        if metadata["name"] == args.job_name:
            raise CheckFailed("That Job name already exists. Inspect it; do not create a replacement automatically.")
        job_labels = job["spec"]["template"].get("metadata", {}).get("labels", {})
        related = (any(x.get("uid") == cron["metadata"]["uid"] for x in metadata.get("ownerReferences", []))
                   or all(job_labels.get(k) == v for k, v in labels.items())
                   or metadata.get("labels", {}).get("backup-verification") == args.cronjob)
        terminal = any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                       for c in status.get("conditions", []))
        if related and not terminal:
            raise CheckFailed("A related backup Job is still pending or running. Stop and inspect it first.")
    job_spec = copy.deepcopy(spec["jobTemplate"]["spec"])
    if job_spec.get("parallelism", 1) != 1 or job_spec.get("completions", 1) != 1:
        raise CheckFailed("The test requires exactly one Pod and one completion.")
    pod = job_spec["template"]["spec"]
    if len(pod["containers"]) != 1 or pod.get("initContainers") or pod.get("restartPolicy") != "Never":
        raise CheckFailed("Expected one backup container with restartPolicy Never and no init containers.")
    container = pod["containers"][0]
    if container.get("name") != "backup":
        raise CheckFailed("Expected the installed backup container.")
    config_name = next(v["configMap"]["name"] for v in pod["volumes"] if v["name"] == "script")
    config = json.loads(kubectl(args, "get", "configmap", config_name, "-o", "json"))
    script_sha = hashlib.sha256(config["data"]["run_backup.py"].encode()).hexdigest()
    container["command"] = ["python3", "-c", POD_SCRIPT]
    container.pop("args", None)
    container["env"].extend({"name": key, "value": value} for key, value in {
        "VERIFY_EXPECTED_FULL": args.expected_full, "VERIFY_TIMEOUT_SECONDS": str(args.timeout_seconds),
        "VERIFY_SCRIPT_SHA256": script_sha}.items())
    job_spec.update(backoffLimit=0, activeDeadlineSeconds=args.timeout_seconds)
    for key in ("selector", "manualSelector", "ttlSecondsAfterFinished"):
        job_spec.pop(key, None)
    metadata = job_spec["template"]["metadata"]
    job_spec["template"]["metadata"] = {k: metadata[k] for k in ("labels", "annotations") if k in metadata}
    return {"apiVersion":"batch/v1", "kind":"Job",
            "metadata":{"name":args.job_name, "namespace":args.namespace,
                        "labels":{"backup-verification":args.cronjob}}, "spec":job_spec}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for field in ("context", "namespace", "cronjob", "expected-full", "job-name"):
        parser.add_argument("--" + field, required=True)
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    parser.add_argument("--run", action="store_true")
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[a-z0-9]([-a-z0-9]*[a-z0-9])?", args.job_name) or len(args.job_name) > 63:
            raise CheckFailed("Use a Kubernetes Job name of at most 63 lowercase letters, digits, and hyphens.")
        if not re.fullmatch(r"ao-otel-full-\d{8}T\d{6}Z-[0-9a-f]{8}", args.expected_full):
            raise CheckFailed("Expected-full must be an exact full-backup name from this scheduler.")
        if not 60 <= args.timeout_seconds <= 3600:
            raise CheckFailed("Timeout must be between 60 and 3600 seconds.")
        manifest = prepare(args, datetime.now(timezone.utc))
        directory = args.output_dir or Path(tempfile.mkdtemp(prefix="ao-backup-failover-"))
        if directory.is_symlink():
            raise CheckFailed("The evidence directory must not be a symbolic link.")
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        directory.chmod(0o700)
        path = directory / (args.job_name + (".run.json" if args.run else ".prepared.json"))
        prepared = directory / (args.job_name + ".prepared.json")
        if args.run and prepared.exists() and json.loads(prepared.read_text()) != manifest:
            raise CheckFailed("The live Job template or scheduler changed after preparation. Stop and review it.")
        with open(path, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as output:
            json.dump(manifest, output, indent=2)
        print(json.dumps({"prepared":str(path), "job":args.job_name, "run":args.run}), flush=True)
        if not args.run:
            return 0
        kubectl(args, "create", "-f", str(path))  # Exactly once; never apply or retry.
        deadline = time.monotonic() + args.timeout_seconds + 60
        while time.monotonic() < deadline:
            job = json.loads(kubectl(args, "get", "job", args.job_name, "-o", "json"))
            conditions = job.get("status", {}).get("conditions", [])
            if any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True" for c in conditions):
                lines = kubectl(args, "logs", "job/" + args.job_name, "-c", "backup").splitlines()
                evidence = [json.loads(line[len("EVIDENCE "):]) for line in lines if line.startswith("EVIDENCE ")]
                if len(evidence) != 1:
                    raise CheckFailed("No single final evidence record was found. Stop and inspect the Job.")
                complete = (any(c.get("type") == "Complete" and c.get("status") == "True" for c in conditions)
                            and not any(c.get("type") == "Failed" and c.get("status") == "True" for c in conditions)
                            and job.get("status", {}).get("succeeded") == 1)
                summary = dict(evidence[0], job=args.job_name, context=args.context, namespace=args.namespace,
                               started=job["status"].get("startTime"), completed=job["status"].get("completionTime"),
                               scheduler_sha256=next(e["value"] for e in manifest["spec"]["template"]["spec"]["containers"][0]["env"] if e["name"] == "VERIFY_SCRIPT_SHA256"))
                if not complete:
                    summary.update(status="inconclusive", reason="The Job did not finish successfully. Stop and inspect; do not rerun automatically.")
                with open(directory / (args.job_name + ".evidence.json"), "x", opener=lambda p, f: os.open(p, f, 0o600)) as output:
                    json.dump(summary, output, indent=2)
                print(json.dumps(summary, sort_keys=True))
                return 0 if complete and summary.get("status") == "passed" else 1
            time.sleep(5)
        raise CheckFailed("Timed out. The backup may still be running. Stop and inspect; do not rerun automatically.")
    except (CheckFailed, OSError, ValueError, KeyError, StopIteration, subprocess.TimeoutExpired) as error:
        print(str(error) if isinstance(error, CheckFailed) else "Verification stopped. Inspect the Job and settings; do not retry automatically.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
