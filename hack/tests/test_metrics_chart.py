"""Render checks for the opt-in Prometheus endpoints.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH.
"""

import copy
import unittest

import yaml

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

        default, enabled = by_key(self.default), by_key(self.enabled)
        self.assertEqual(enabled.keys(), default.keys())
        for key, document in default.items():
            if key[0] != "ClickHouseInstallation":
                self.assertEqual(document, enabled[key], key)


SCRAPERS = [{
    "namespaceSelector": {"matchLabels": {"kubernetes.io/metadata.name": "monitoring"}},
    "podSelector": {"matchLabels": {"app.kubernetes.io/name": "prometheus"}},
}]
SCRAPERS_OVERRIDE = "keeper.metrics.networkPolicyFrom[0].namespaceSelector.matchLabels.kubernetes\\.io/metadata\\.name=monitoring"
SCRAPERS_POD_OVERRIDE = "keeper.metrics.networkPolicyFrom[0].podSelector.matchLabels.app\\.kubernetes\\.io/name=prometheus"


def keeper_settings(documents):
    return chart.one(documents, "ClickHouseKeeperInstallation")["spec"]["configuration"].get("settings")


def keeper_ingress(documents):
    return chart.one(documents, "NetworkPolicy", "keeper-otel")["spec"]["ingress"]


def rule_ports(rule):
    return {port["port"] for port in rule["ports"]}


class KeeperMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.default = render()
        cls.enabled = render("keeper.metrics.enabled=true", SCRAPERS_OVERRIDE, SCRAPERS_POD_OVERRIDE)
        cls.chk_off = chart.one(cls.default, "ClickHouseKeeperInstallation")
        cls.chk_on = chart.one(cls.enabled, "ClickHouseKeeperInstallation")

    def test_metrics_are_off_by_default(self):
        self.assertEqual(keeper_settings(self.default),
                         {"keeper_server/four_letter_word_allow_list": "ruok,mntr,srvr,stat,conf"})
        self.assertNotIn("ports", container(self.chk_off, "clickhouse-keeper"))
        self.assertEqual({p for rule in keeper_ingress(self.default) for p in rule_ports(rule)}, {2181, 9444})

    def test_enabling_serves_metrics_on_each_voter(self):
        self.assertEqual(keeper_settings(self.enabled), {
            "keeper_server/four_letter_word_allow_list": "ruok,mntr,srvr,stat,conf",
            **PROMETHEUS_SETTINGS,
        })
        self.assertEqual(container(self.chk_on, "clickhouse-keeper")["ports"], [METRICS_PORT])

    def test_empty_allow_list_still_falls_back_to_keepers_default(self):
        # No settings key at all keeps the CHK unchanged for installs using neither option.
        self.assertIsNone(keeper_settings(render("keeper.fourLetterWordAllowList=")))
        self.assertEqual(
            keeper_settings(render("keeper.fourLetterWordAllowList=", "keeper.metrics.enabled=true",
                                   SCRAPERS_OVERRIDE, SCRAPERS_POD_OVERRIDE)),
            PROMETHEUS_SETTINGS)

    def test_scrapers_reach_metrics_but_never_the_client_port(self):
        default_rules = keeper_ingress(self.default)
        rules = keeper_ingress(self.enabled)
        self.assertEqual(rules[:len(default_rules)], default_rules)
        metrics_rules = [rule for rule in rules if 9363 in rule_ports(rule)]
        self.assertEqual(metrics_rules, [{"from": SCRAPERS, "ports": [{"protocol": "TCP", "port": 9363}]}])
        client_rules = [rule for rule in rules if 2181 in rule_ports(rule)]
        self.assertEqual(len(client_rules), 1)
        self.assertNotIn(SCRAPERS[0], client_rules[0]["from"])

    def test_enabling_without_scrapers_fails_while_the_policy_is_on(self):
        with self.assertRaisesRegex(AssertionError, "keeper.metrics.networkPolicyFrom is empty"):
            render("keeper.metrics.enabled=true")
        documents = render("keeper.metrics.enabled=true", "keeper.networkPolicy.enabled=false")
        self.assertFalse([d for d in documents if d["kind"] == "NetworkPolicy"
                          and d["metadata"]["name"].startswith("keeper-")])
        self.assertEqual(keeper_settings(documents)["prometheus/port"], "9363")

    def test_enabling_changes_only_keeper_objects(self):
        default, enabled = by_key(self.default), by_key(self.enabled)
        self.assertEqual(enabled.keys(), default.keys())
        for key, document in default.items():
            if key not in {("ClickHouseKeeperInstallation", "otel"), ("NetworkPolicy", "keeper-otel")}:
                self.assertEqual(document, enabled[key], key)


SCRAPE_ANNOTATIONS = {"prometheus.io/scrape": "true", "prometheus.io/port": "9363"}
REVISION_ANNOTATION = "backup.montecarlodata.com/password-revision"


def render_annotations(component_annotations, backup=False):
    """Render with podAnnotations passed through a values file, as users set them."""
    return chart.render(backup=backup, values={component: {"podAnnotations": annotations}
                                               for component, annotations in component_annotations.items()})


def pod_annotations(installation):
    return installation["spec"]["templates"]["podTemplates"][0].get("metadata", {}).get("annotations")


class PodAnnotationTests(unittest.TestCase):
    def test_no_annotations_by_default(self):
        documents = render()
        for kind in ("ClickHouseInstallation", "ClickHouseKeeperInstallation"):
            self.assertIsNone(pod_annotations(chart.one(documents, kind)), kind)

    def test_user_annotations_reach_both_pod_templates(self):
        documents = render_annotations({"clickhouse": SCRAPE_ANNOTATIONS, "keeper": SCRAPE_ANNOTATIONS})
        for kind in ("ClickHouseInstallation", "ClickHouseKeeperInstallation"):
            self.assertEqual(pod_annotations(chart.one(documents, kind)), SCRAPE_ANNOTATIONS, kind)

    def test_non_string_annotation_values_render_as_strings(self):
        # The operator decodes these into ObjectMeta, whose annotations are strings;
        # a number or bool stops it from listing any installation.
        expected = {"prometheus.io/port": "9363", "prometheus.io/scrape": "true"}
        from_file = render_annotations({component: {"prometheus.io/port": 9363, "prometheus.io/scrape": True}
                                        for component in ("clickhouse", "keeper")})
        for kind in ("ClickHouseInstallation", "ClickHouseKeeperInstallation"):
            self.assertEqual(pod_annotations(chart.one(from_file, kind)), expected, f"values file, {kind}")

    def test_non_string_annotation_overrides_render_as_strings(self):
        expected = {"prometheus.io/port": "9363", "prometheus.io/scrape": "true"}
        overrides = [f"{component}.podAnnotations.prometheus\\.io/{key}={value}"
                     for component in ("clickhouse", "keeper")
                     for key, value in (("port", "9363"), ("scrape", "true"))]
        documents = render(*overrides)
        for kind in ("ClickHouseInstallation", "ClickHouseKeeperInstallation"):
            self.assertEqual(pod_annotations(chart.one(documents, kind)), expected, f"--set, {kind}")

    def test_user_annotations_merge_with_the_backup_revision(self):
        revision = pod_annotations(chart.one(chart.render(), "ClickHouseInstallation"))[REVISION_ANNOTATION]
        documents = render_annotations({"clickhouse": SCRAPE_ANNOTATIONS}, backup=True)
        self.assertEqual(pod_annotations(chart.one(documents, "ClickHouseInstallation")),
                         {**SCRAPE_ANNOTATIONS, REVISION_ANNOTATION: revision})

    def test_chart_owned_backup_annotations_are_rejected(self):
        for backup in (True, False):
            with self.subTest(backup=backup):
                with self.assertRaisesRegex(AssertionError, "backup.montecarlodata.com/"):
                    render_annotations({"clickhouse": {REVISION_ANNOTATION: "rev-1"}}, backup=backup)


class CollectorMetricsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        documents = render()
        cls.config = yaml.safe_load(chart.one(documents, "ConfigMap", "opentelemetry-collector")["data"]["relay"])
        cls.service = chart.one(documents, "Service", "opentelemetry-collector")

    def test_collector_does_not_scrape_itself(self):
        for name, pipeline in self.config["service"]["pipelines"].items():
            self.assertNotIn("prometheus", pipeline["receivers"], name)

    def test_collector_still_serves_its_own_metrics_on_the_pod_ip(self):
        readers = self.config["service"]["telemetry"]["metrics"]["readers"]
        self.assertIn({"pull": {"exporter": {"prometheus": {"host": "${env:MY_POD_IP}", "port": 8888}}}},
                      readers)

    def test_collector_metrics_stay_off_the_service(self):
        # The Service can sit behind a public load balancer, so 8888 is never added to it.
        self.assertEqual([port["port"] for port in self.service["spec"]["ports"]], [4317, 4318])


if __name__ == "__main__":
    unittest.main()
