"""Render checks for the opt-in Prometheus endpoints.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH.
"""

import copy
import unittest

# Imported as a module so unittest does not collect its test class a second time.
import test_backup_chart as chart


PROMETHEUS_SETTINGS = {
    "prometheus/endpoint": "/metrics",
    "prometheus/port": "9363",
    "prometheus/metrics": "true",
    "prometheus/events": "true",
    "prometheus/asynchronous_metrics": "true",
}
METRICS_PORT = {"name": "metrics", "containerPort": 9363, "protocol": "TCP"}


def render(*overrides):
    return chart.render(*overrides, backup=False)


def by_key(documents):
    return {(d["kind"], d["metadata"]["name"]): d for d in documents}


def container(chi, name):
    return next(c for c in chart.pod(chi)["containers"] if c["name"] == name)


class ClickHouseMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = render()
        cls.enabled = render("clickhouse.metrics.enabled=true")
        cls.chi_off = chart.one(cls.default, "ClickHouseInstallation")
        cls.chi_on = chart.one(cls.enabled, "ClickHouseInstallation")

    def test_metrics_are_off_by_default(self):
        settings = self.chi_off["spec"]["configuration"]["settings"]
        self.assertFalse([key for key in settings if key.startswith("prometheus/")])
        self.assertNotIn("ports", container(self.chi_off, "clickhouse"))

    def test_enabling_serves_metrics_on_each_replica(self):
        settings = self.chi_on["spec"]["configuration"]["settings"]
        self.assertEqual({k: v for k, v in settings.items() if k.startswith("prometheus/")},
                         PROMETHEUS_SETTINGS)
        self.assertEqual(container(self.chi_on, "clickhouse")["ports"], [METRICS_PORT])

    def test_metrics_stay_off_the_client_service(self):
        for chi in (self.chi_off, self.chi_on):
            for service in chi["spec"]["templates"]["serviceTemplates"]:
                self.assertNotIn(9363, [port["port"] for port in service["spec"]["ports"]])
        self.assertEqual(self.chi_off["spec"]["templates"]["serviceTemplates"],
                         self.chi_on["spec"]["templates"]["serviceTemplates"])

    def test_enabling_changes_only_the_endpoint_settings_and_port(self):
        # Strip exactly what the flag adds; everything else must match the default.
        stripped = copy.deepcopy(self.chi_on)
        settings = stripped["spec"]["configuration"]["settings"]
        for key in PROMETHEUS_SETTINGS:
            del settings[key]
        del container(stripped, "clickhouse")["ports"]
        self.assertEqual(stripped, self.chi_off)

        enabled = by_key(self.enabled)
        for key, document in by_key(self.default).items():
            if key[0] != "ClickHouseInstallation":
                self.assertEqual(document, enabled[key], key)


if __name__ == "__main__":
    unittest.main()
