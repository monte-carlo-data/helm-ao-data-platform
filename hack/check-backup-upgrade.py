#!/usr/bin/env python3
"""Read-only gate before setting backup.migration.keepSharedCredentials=false.

Run after the first migration apply, with backups still suspended. This checks
operator 0.26's completed generation, both StatefulSet revisions, and files in
both ClickHouse containers. It never reads a Secret or changes any resource.
Success is a point-in-time check; keep the schedule paused for the second apply.
"""

import argparse
import json
import re
import subprocess
import sys


class CheckFailed(Exception):
    pass


# Only exit codes leave the Pod. Never print auth files or secret contents.
FILE_CHECK = r'''
set -eu
main=/etc/clickhouse-server/users.d/auth-methods.xml
stores=/etc/clickhouse-server/config.d/backup-users.xml
test -s "$main"
if grep -q 'backup_auth_methods' "$main"; then exit 1; fi
test -s "$stores"
grep -Fq '/etc/clickhouse-backup-auth/user/users.xml' "$stores"
grep -Fq '/etc/clickhouse-backup-auth/probe/users.xml' "$stores"
for kind in user probe; do
  test -s "/etc/clickhouse-backup-auth/$kind/users.xml"
  test -r "/etc/clickhouse-backup-auth/$kind/users.d/auth.xml"
done
'''


def require(condition, reason):
    if not condition:
        raise CheckFailed(reason)


def kubectl(args, *command):
    try:
        result = subprocess.run(
            ["kubectl", "--context", args.context, "--namespace", args.namespace, *command],
            text=True, capture_output=True, timeout=40,
        )
    except (OSError, subprocess.TimeoutExpired):
        raise CheckFailed("kubectl could not finish the read-only check.") from None
    # Suppress all command output on failure, including kubectl/exec diagnostics.
    require(result.returncode == 0, "A read-only Kubernetes or Pod file check failed.")
    return result.stdout


def get(args, *resource):
    return json.loads(kubectl(args, "get", *resource, "-o", "json"))


def container(spec, name):
    matches = [c for c in spec.get("containers", []) if c.get("name") == name]
    require(len(matches) == 1, "Expected exactly one ClickHouse container.")
    return matches[0]


def check_mounts(pod):
    spec = pod["spec"]
    mounts = {m["mountPath"]: m for m in container(spec, "clickhouse").get("volumeMounts", [])}
    volumes = {v["name"]: v for v in spec.get("volumes", [])}
    for path in ("/etc/clickhouse-backup-auth/user", "/etc/clickhouse-backup-auth/probe"):
        mount = mounts.get(path, {})
        require(mount.get("readOnly") is True and "subPath" not in mount,
                "A Pod lacks the whole read-only backup authentication mount.")
        projected = volumes.get(mount.get("name"), {}).get("projected", {})
        sources = projected.get("sources", [])
        require(any("configMap" in source for source in sources)
                and any(source.get("secret", {}).get("optional") is True
                        and source["secret"].get("items") == [{"key": "auth.xml", "path": "users.d/auth.xml"}]
                        for source in sources),
                "A Pod lacks the empty user-store ConfigMap and optional backup Secret projection.")
    mount = mounts.get("/etc/clickhouse-server/config.d/", {})
    sources = volumes.get(mount.get("name"), {}).get("projected", {}).get("sources", [])
    require(mount.get("readOnly") is True and "subPath" not in mount
            and len(sources) == 2 and all("configMap" in source for source in sources)
            and any({"key": "backup-users.xml", "path": "backup-users.xml"}
                    in source["configMap"].get("items", []) for source in sources),
            "The backup user-store config is not mounted through the new Pod template.")


def check_snapshot(chi, storage, cron, pods, sets, jobs):
    metadata, status = chi["metadata"], chi.get("status", {})
    require(status.get("status") == "Completed" and not status.get("hostsFailed", 0),
            "The ClickHouse operator has not completed the rollout successfully.")
    require(bool(status.get("taskID"))
            and status.get("taskIDsCompleted", [None])[0] == status["taskID"],
            "The latest ClickHouse operator task is not completed.")
    completed = json.loads(storage.get("data", {}).get("status-normalizedCompleted", "{}"))
    require(completed.get("metadata", {}).get("generation") == metadata.get("generation")
            and metadata.get("generation") is not None,
            "The completed ClickHouse configuration is from an older generation.")
    require(cron["spec"].get("suspend") is True, "The backup schedule must remain paused.")
    require(not cron.get("status", {}).get("active"), "A scheduled backup Job is still active.")
    labels = cron["spec"]["jobTemplate"]["spec"]["template"].get("metadata", {}).get("labels", {})
    for job in jobs:
        related = (any(o.get("uid") == cron["metadata"]["uid"]
                       for o in job["metadata"].get("ownerReferences", []))
                   or (bool(labels) and all(job["spec"]["template"].get("metadata", {}).get("labels", {}).get(k) == v
                                           for k, v in labels.items()))
                   or job["metadata"].get("labels", {}).get("backup-verification") == cron["metadata"]["name"])
        terminal = any(c.get("type") in ("Complete", "Failed") and c.get("status") == "True"
                       for c in job.get("status", {}).get("conditions", []))
        require(not related or terminal, "A backup Job is pending or running; drain it before finishing migration.")
    require(len(pods) == 2 and len(sets) == 2, "Expected exactly two ClickHouse Pods and two StatefulSets.")
    by_uid = {s["metadata"]["uid"]: s for s in sets}
    owners_seen = set()
    for pod in pods:
        name = pod["metadata"]["name"]
        require(not pod["metadata"].get("deletionTimestamp") and pod.get("status", {}).get("phase") == "Running"
                and any(c.get("type") == "Ready" and c.get("status") == "True"
                        for c in pod.get("status", {}).get("conditions", [])),
                f"Pod {name} is not Ready or is being replaced.")
        expected = {c["name"] for c in pod["spec"]["containers"]}
        ready = {c["name"] for c in pod.get("status", {}).get("containerStatuses", [])
                 if c.get("ready") and "running" in c.get("state", {})}
        require(expected == ready, f"Pod {name} has an unready container.")
        owners = [o for o in pod["metadata"].get("ownerReferences", [])
                  if o.get("kind") == "StatefulSet" and o.get("controller") is True]
        require(len(owners) == 1 and owners[0]["uid"] in by_uid, "A Pod has an unexpected StatefulSet owner.")
        uid = owners[0]["uid"]
        require(uid not in owners_seen, "Both Pods belong to the same StatefulSet.")
        owners_seen.add(uid)
        stateful = by_uid[uid]
        st = stateful.get("status", {})
        require(not stateful["metadata"].get("deletionTimestamp")
                and stateful["spec"].get("replicas") == 1
                and st.get("observedGeneration") == stateful["metadata"].get("generation")
                and all(st.get(k) == 1 for k in ("readyReplicas", "currentReplicas", "updatedReplicas"))
                and bool(st.get("updateRevision")) and st.get("currentRevision") == st["updateRevision"]
                and pod["metadata"].get("labels", {}).get("controller-revision-hash") == st["updateRevision"],
                f"Pod {name} has not reached its current StatefulSet revision.")
        check_mounts(pod)
    return sorted(p["metadata"]["name"] for p in pods)


def snapshot(args):
    selector = "clickhouse.altinity.com/chi=" + args.chi
    return (get(args, "chi", args.chi), get(args, "configmap", "chi-storage-" + args.chi),
            get(args, "cronjob", args.cronjob), get(args, "pods", "-l", selector)["items"],
            get(args, "statefulsets", "-l", selector)["items"], get(args, "jobs")["items"])


def fingerprint(values):
    # Jobs may naturally finish during the check; the second full validation still
    # rejects any active work. Watch all resources that establish Pod readiness.
    resources = list(values[:3]) + values[3] + values[4]
    return sorted((o["kind"], o["metadata"]["name"], o["metadata"]["uid"],
                   o["metadata"]["resourceVersion"]) for o in resources)


def check(args):
    before = snapshot(args)
    names = check_snapshot(*before)
    for name in names:
        kubectl(args, "exec", name, "-c", "clickhouse", "--", "/bin/sh", "-c", FILE_CHECK)
    after = snapshot(args)
    check_snapshot(*after)
    require(fingerprint(before) == fingerprint(after), "The rollout changed during the check; inspect it and check again.")
    return {"status": "passed", "pods": names, "schedule_paused": True,
            "instruction": "Both copies have the new files. Keep backups paused while finishing migration."}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", required=True)
    parser.add_argument("--namespace", required=True)
    parser.add_argument("--chi", required=True)
    parser.add_argument("--cronjob", required=True)
    args = parser.parse_args(argv)
    for name in (args.namespace, args.chi, args.cronjob):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?", name):
            parser.error("Kubernetes names must contain lowercase letters, digits, dots, or hyphens.")
    try:
        result = check(args)
    except CheckFailed as error:
        print(json.dumps({"status": "refused", "reason": str(error)}))
        return 1
    except (ValueError, KeyError, TypeError, IndexError):
        print(json.dumps({"status": "refused", "reason": "Unexpected Kubernetes data; migration readiness could not be verified."}))
        return 1
    print(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
