"""Check the collector's JSON settings and rollout in the rendered chart."""

import unittest
from urllib.parse import parse_qs, urlsplit

import yaml
from runtime.test_code_agent_feedback_runtime import COLLECTOR_JSON_SETTINGS
from test_backup_chart import render


class FeedbackChartTests(unittest.TestCase):
    def test_custom_endpoint_keeps_both_settings_and_tls(self):
        endpoint = "tcp://custom-clickhouse:9440?secure=true&input_format_try_infer_dates=0&input_format_try_infer_datetimes=0"
        documents = render(
            "opentelemetry-collector.config.exporters.clickhouse.endpoint=" + endpoint,
            backup=False,
        )
        config_map = next(doc for doc in documents if "relay" in (doc.get("data") or {}))
        config = yaml.safe_load(config_map["data"]["relay"])
        self.assertEqual(config["exporters"]["clickhouse"]["endpoint"], endpoint)

    def test_collector_and_runtime_inserts_disable_both_date_inference_settings(self):
        documents = render(backup=False)
        config_map = next(doc for doc in documents if "relay" in (doc.get("data") or {}))
        config = yaml.safe_load(config_map["data"]["relay"])
        exporter = config["exporters"]["clickhouse"]
        settings = parse_qs(urlsplit(exporter["endpoint"]).query)
        self.assertTrue(exporter["json"])
        self.assertEqual(settings["secure"], ["true"])
        for name, value in COLLECTOR_JSON_SETTINGS.items():
            self.assertEqual(settings[name], [value])

        old_documents = render(
            "opentelemetry-collector.config.exporters.clickhouse.endpoint=tcp://clickhouse-otel.{{ .Release.Namespace }}.svc.cluster.local:9440?secure=true",
            backup=False,
        )

        def collector_checksum(docs):
            deployment = next(
                doc
                for doc in docs
                if doc["kind"] == "Deployment"
                and "opentelemetry-collector" in doc["metadata"]["name"]
            )
            return deployment["spec"]["template"]["metadata"]["annotations"][
                "checksum/config"
            ]

        self.assertNotEqual(
            collector_checksum(documents), collector_checksum(old_documents)
        )
