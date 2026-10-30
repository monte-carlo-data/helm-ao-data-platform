"""Check the collector's JSON settings and rollout in the rendered chart."""

import unittest
from urllib.parse import parse_qs, urlsplit

import yaml
from runtime.test_code_agent_feedback_runtime import COLLECTOR_JSON_SETTINGS
from test_backup_chart import render


class FeedbackChartTests(unittest.TestCase):
    def test_tls_off_endpoint_requires_both_settings(self):
        endpoint = "tcp://custom-clickhouse:9000"
        with self.assertRaisesRegex(AssertionError, "input_format_try_infer_dates=0"):
            render(
                "tls.enabled=false",
                "gateway.enabled=false",
                "opentelemetry-collector.config.exporters.clickhouse.endpoint="
                + endpoint,
                backup=False,
            )

    def test_rejects_missing_nonzero_duplicate_and_fragment_settings(self):
        for query in (
            "input_format_try_infer_dates=0",
            "input_format_try_infer_datetimes=0",
            "input_format_try_infer_dates=1&input_format_try_infer_datetimes=0",
            "input_format_try_infer_dates=0&input_format_try_infer_datetimes=1",
            "input_format_try_infer_dates=1&input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
            "input_format_try_infer_dates=0&input_format_try_infer_datetimes=0&input_format_try_infer_datetimes=0",
            "secure=true#input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
            "%69nput_format_try_infer_dates=1&input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
        ):
            with (
                self.subTest(query=query),
                self.assertRaisesRegex(
                    AssertionError, "input_format_try_infer_dates=0"
                ),
            ):
                render(
                    "opentelemetry-collector.config.exporters.clickhouse.endpoint=tcp://custom-clickhouse:9440?"
                    + query,
                    backup=False,
                )

    def test_alternate_config_checks_the_active_endpoint(self):
        with self.assertRaisesRegex(AssertionError, "input_format_try_infer_dates=0"):
            render(
                backup=False,
                values={
                    "opentelemetry-collector": {
                        "alternateConfig": {
                            "exporters": {
                                "clickhouse/custom": {
                                    "endpoint": "tcp://custom-clickhouse:9000"
                                }
                            },
                            "service": {
                                "pipelines": {
                                    "traces": {"exporters": ["clickhouse/custom"]}
                                }
                            },
                        }
                    }
                },
            )

    def test_external_config_requires_explicit_verification(self):
        for overrides in (
            (
                "opentelemetry-collector.configMap.create=false",
                "opentelemetry-collector.configMap.existingName=custom-collector",
            ),
            ("opentelemetry-collector.configMap.create=false",),
        ):
            with self.subTest(overrides=overrides):
                with self.assertRaisesRegex(AssertionError, "Helm cannot inspect"):
                    render(*overrides, backup=False)
                documents = render(
                    *overrides,
                    "collectorEndpointValidation.externalConfigVerified=true",
                    backup=False,
                )
                self.assertFalse(
                    any("relay" in (doc.get("data") or {}) for doc in documents)
                )

    def test_external_confirmation_does_not_bypass_generated_config_check(self):
        with self.assertRaisesRegex(AssertionError, "input_format_try_infer_dates=0"):
            render(
                "collectorEndpointValidation.externalConfigVerified=true",
                "opentelemetry-collector.config.exporters.clickhouse.endpoint=tcp://custom-clickhouse:9000",
                backup=False,
            )

    def test_external_confirmation_requires_a_boolean(self):
        for value in ("false", "true", 1):
            with (
                self.subTest(value=value),
                self.assertRaisesRegex(AssertionError, "Helm cannot inspect"),
            ):
                render(
                    "opentelemetry-collector.configMap.create=false",
                    backup=False,
                    values={
                        "collectorEndpointValidation": {"externalConfigVerified": value}
                    },
                )

    def test_unused_exporters_do_not_block_other_pipelines(self):
        config = {
            "exporters": {
                "debug": {},
                "clickhouse/unused": {"endpoint": "tcp://unused:9000"},
            },
            "service": {"pipelines": {"traces": {"exporters": ["debug"]}}},
        }
        documents = render(
            backup=False,
            values={"opentelemetry-collector": {"alternateConfig": config}},
        )
        config_map = next(
            doc for doc in documents if "relay" in (doc.get("data") or {})
        )
        self.assertEqual(yaml.safe_load(config_map["data"]["relay"]), config)

    def test_every_active_clickhouse_exporter_is_checked(self):
        with self.assertRaisesRegex(AssertionError, "input_format_try_infer_dates=0"):
            render(
                backup=False,
                values={
                    "opentelemetry-collector": {
                        "config": {
                            "exporters": {
                                "clickhouse/other": {"endpoint": "tcp://other:9000"}
                            },
                            "service": {
                                "pipelines": {
                                    "traces": {
                                        "exporters": ["clickhouse", "clickhouse/other"]
                                    }
                                }
                            },
                        }
                    }
                },
            )

    def test_templates_and_all_workload_modes_use_the_effective_endpoint(self):
        endpoint = "tcp://{{ .Values.fullnameOverride }}.{{ .Release.Namespace }}:9000?input_format_try_infer_dates=0&input_format_try_infer_datetimes=0"
        for mode in ("deployment", "daemonset", "statefulset"):
            with self.subTest(mode=mode):
                documents = render(
                    "opentelemetry-collector.mode=" + mode,
                    "opentelemetry-collector.config.exporters.clickhouse.endpoint="
                    + endpoint,
                    backup=False,
                )
                config_map = next(
                    doc for doc in documents if "relay" in (doc.get("data") or {})
                )
                config = yaml.safe_load(config_map["data"]["relay"])
                self.assertEqual(
                    config["exporters"]["clickhouse"]["endpoint"],
                    "tcp://opentelemetry-collector.montecarlo:9000?input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
                )

    def test_custom_endpoint_keeps_both_settings_and_tls(self):
        for endpoint in (
            "tcp://custom-clickhouse:9440?secure=true&skip_verify=false&input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
            "tcp://custom-clickhouse:9000?input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
        ):
            with self.subTest(endpoint=endpoint):
                documents = render(
                    "opentelemetry-collector.config.exporters.clickhouse.endpoint="
                    + endpoint,
                    backup=False,
                )
                config_map = next(
                    doc for doc in documents if "relay" in (doc.get("data") or {})
                )
                config = yaml.safe_load(config_map["data"]["relay"])
                self.assertEqual(
                    config["exporters"]["clickhouse"]["endpoint"], endpoint
                )

    def test_collector_and_runtime_inserts_disable_both_date_inference_settings(self):
        documents = render(backup=False)
        config_map = next(
            doc for doc in documents if "relay" in (doc.get("data") or {})
        )
        config = yaml.safe_load(config_map["data"]["relay"])
        exporter = config["exporters"]["clickhouse"]
        settings = parse_qs(urlsplit(exporter["endpoint"]).query)
        self.assertTrue(exporter["json"])
        self.assertEqual(settings["secure"], ["true"])
        for name, value in COLLECTOR_JSON_SETTINGS.items():
            self.assertEqual(settings[name], [value])

        changed_documents = render(
            "opentelemetry-collector.config.exporters.clickhouse.endpoint=tcp://custom-clickhouse:9440?secure=true&input_format_try_infer_dates=0&input_format_try_infer_datetimes=0",
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
            collector_checksum(documents), collector_checksum(changed_documents)
        )
