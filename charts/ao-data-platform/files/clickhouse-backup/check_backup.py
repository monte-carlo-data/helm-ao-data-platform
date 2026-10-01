#!/usr/bin/env python3
"""Read scheduled-job health and send metrics; CloudWatch sends the alerts.

This job has no backup password or permission to change Kubernetes objects.
CloudWatch also alerts when these reports stop arriving, including cluster loss.
"""

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import ssl
import sys
import urllib.parse
import urllib.request


class MonitorError(Exception):
    pass


def timestamp(value):
    if not isinstance(value, str):
        raise MonitorError("Missing or invalid job timestamp.")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise MonitorError("Missing or invalid job timestamp.") from None
    if result.tzinfo is None:
        raise MonitorError("Job timestamp has no time zone.")
    return result


def evaluate(cronjob, jobs, now, max_age_seconds):
    """Only this CronJob's completed runs count; manually created jobs do not."""
    metadata = cronjob["metadata"]
    uid, name = metadata["uid"], metadata["name"]
    if not uid or not name or max_age_seconds <= 0:
        raise MonitorError("Invalid schedule identity or time allowance.")
    # kubectl's manual --from=cronjob Jobs have this same owner reference, and
    # Kubernetes also counts their completion in CronJob.lastSuccessfulTime.
    # Read retained scheduled Jobs instead. They survive monitor/pod restarts.
    scheduled = []
    for job in jobs:
        job_metadata = job.get("metadata", {})
        owners = job_metadata.get("ownerReferences", [])
        if not any(owner.get("kind") == "CronJob" and owner.get("uid") == uid
                   and owner.get("name") == name and owner.get("controller") is True
                   for owner in owners):
            continue
        annotations = job_metadata.get("annotations", {})
        if annotations.get("cronjob.kubernetes.io/instantiate") == "manual":
            continue
        scheduled_at = annotations.get("batch.kubernetes.io/cronjob-scheduled-timestamp")
        if scheduled_at:
            if timestamp(scheduled_at) > now:
                raise MonitorError("Scheduled job timestamp is in the future.")
        elif not re.fullmatch(re.escape(name) + r"-[0-9]+", job_metadata.get("name", "")):
            # Before Kubernetes 1.32, the controller did not set that annotation.
            # It names scheduled jobs <cronjob>-<scheduled Unix time in minutes>.
            continue
        scheduled.append(job)

    completed = [timestamp(job.get("status", {}).get("completionTime")) for job in scheduled
                 if any(condition.get("type") == "Complete" and condition.get("status") == "True"
                        for condition in job.get("status", {}).get("conditions", []))]
    last_success = max(completed, default=None)
    start = last_success or timestamp(metadata["creationTimestamp"])
    if start > now:
        raise MonitorError("Schedule timestamp is in the future.")
    overdue = (now - start).total_seconds() > max_age_seconds
    # A paused schedule also needs attention; continuing manual backups cannot hide it.
    overdue = overdue or bool(cronjob.get("spec", {}).get("suspend", False))
    failed = False
    for job in scheduled:
        for condition in job.get("status", {}).get("conditions", []):
            if condition.get("type") == "Failed" and condition.get("status") == "True":
                ended = timestamp(condition.get("lastTransitionTime"))
                if ended > now:
                    raise MonitorError("Failed job timestamp is in the future.")
                failed = failed or last_success is None or ended > last_success
    return {"BackupJobFailed": int(failed), "BackupOverdue": int(overdue), "MonitorHealthy": 1}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Kubernetes:
    def __init__(self, namespace, credentials="/var/run/secrets/kubernetes.io/serviceaccount"):
        self.namespace = urllib.parse.quote(namespace, safe="")
        self.credentials = Path(credentials)
        context = ssl.create_default_context(cafile=str(self.credentials / "ca.crt"))
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def get(self, path, query=None):
        url = "https://kubernetes.default.svc" + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        request = urllib.request.Request(url, headers={
            "Authorization": "Bearer " + (self.credentials / "token").read_text().strip(),
            "Accept": "application/json",
        })
        with self.opener.open(request, timeout=15) as response:
            data = response.read(8 * 1024 * 1024 + 1)
        if len(data) > 8 * 1024 * 1024:
            raise MonitorError("Kubernetes response is too large.")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise MonitorError("Kubernetes returned an unexpected response.")
        return result

    def snapshot(self, name):
        path = "/apis/batch/v1/namespaces/" + self.namespace
        cronjob = self.get(path + "/cronjobs/" + urllib.parse.quote(name, safe=""))
        jobs, cursor, seen = [], "", set()
        for _ in range(20):
            page = self.get(path + "/jobs", {"limit": 100, "continue": cursor})
            if not isinstance(page.get("items"), list):
                raise MonitorError("Kubernetes returned an invalid job list.")
            jobs.extend(page["items"])
            cursor = page.get("metadata", {}).get("continue", "")
            if not cursor:
                return cronjob, jobs
            if cursor in seen:
                break
            seen.add(cursor)
        raise MonitorError("Could not read the complete job list.")


def check(reader, name, now, max_age_seconds):
    try:
        cronjob, jobs = reader.snapshot(name)
        return evaluate(cronjob, jobs, now, max_age_seconds)
    except Exception:
        # Do not log API bodies, service-account tokens, or AWS credentials.
        print("Could not read valid scheduled-backup status; reporting monitor failure.", file=sys.stderr)
        # Backup status is unknown, not healthy. Omit those metrics so their
        # alarms retain their last state; report the monitor failure separately.
        return {"MonitorHealthy": 0}


def publish(client, metrics, cluster, namespace, cronjob):
    dimensions = [{"Name": key, "Value": value} for key, value in
                  (("Cluster", cluster), ("Namespace", namespace), ("CronJob", cronjob))]
    client.put_metric_data(Namespace="AO/ClickHouseBackup", MetricData=[
        {"MetricName": name, "Dimensions": dimensions, "Value": value, "Unit": "Count"}
        for name, value in metrics.items()
    ])


def main():
    try:
        # AWS's Python base image supplies this SDK; no runtime package download.
        import boto3
        from botocore.config import Config

        namespace = os.environ["POD_NAMESPACE"]
        name = os.environ["BACKUP_CRONJOB"]
        cluster = os.environ["CLUSTER_NAME"]
        max_age = int(os.environ.get("BACKUP_MAX_AGE_SECONDS", "15300"))
        client = boto3.client("cloudwatch", region_name=os.environ["AWS_REGION"],
                              config=Config(connect_timeout=10, read_timeout=15,
                                            retries={"max_attempts": 2, "mode": "standard"}))
        metrics = check(Kubernetes(namespace), name, datetime.now(timezone.utc), max_age)
        publish(client, metrics, cluster, namespace, name)
        print(json.dumps(metrics, sort_keys=True), flush=True)
        return 0 if metrics["MonitorHealthy"] else 1
    except Exception:
        print("Backup monitor could not report health; check its settings and access. "
              "The missing-report alarm covers this failure.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
