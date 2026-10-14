"""Render checks that values reaching the ClickHouse and Keeper installations keep
the types the operator decodes them into. Why that matters: the type-normalization
helpers in charts/ao-data-platform/templates/_helpers.tpl.

Run after `helm dependency build charts/ao-data-platform` with PyYAML installed.
Set HELM to use a Helm binary outside PATH.
"""

import unittest

# Imported as a module so unittest does not collect its test class a second time.
import test_backup_chart as chart


CHI = "ClickHouseInstallation"
CHK = "ClickHouseKeeperInstallation"
COMPONENTS = {"clickhouse": CHI, "keeper": CHK}


def render(*overrides, values=None):
    return chart.render(*overrides, backup=False, values=values)


def pod_spec(documents, kind):
    return chart.pod(chart.one(documents, kind))


def service_annotations(documents):
    service = chart.one(documents, CHI)["spec"]["templates"]["serviceTemplates"][0]
    return service["metadata"]["annotations"]


def storage_class(documents, kind):
    claim = chart.one(documents, kind)["spec"]["templates"]["volumeClaimTemplates"][0]
    return claim["spec"]["storageClassName"]


def container_image(documents, kind):
    return pod_spec(documents, kind)["containers"][0]["image"]


class ServiceAnnotationTests(unittest.TestCase):
    EXPECTED = {
        "service.beta.kubernetes.io/aws-load-balancer-internal": "true",
        "service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval": "10",
    }

    def test_values_file_numbers_and_bools_render_as_strings(self):
        documents = render(values={"clickhouse": {"service": {"annotations": {
            "service.beta.kubernetes.io/aws-load-balancer-internal": True,
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval": 10,
        }}}})
        self.assertEqual(service_annotations(documents), self.EXPECTED)

    def test_set_numbers_and_bools_render_as_strings(self):
        documents = render(
            "clickhouse.service.annotations.service\\.beta\\.kubernetes\\.io/aws-load-balancer-internal=true",
            "clickhouse.service.annotations.service\\.beta\\.kubernetes\\.io/aws-load-balancer-healthcheck-interval=10",
        )
        self.assertEqual(service_annotations(documents), self.EXPECTED)

    def test_values_file_large_numbers_render_without_exponent(self):
        # Values-file numbers arrive as float64, and 1e6 and up is where %v switches
        # to exponent form ("2.592e+06").
        documents = render(values={"clickhouse": {"service": {"annotations": {
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval": 2592000,
        }}}})
        self.assertEqual(service_annotations(documents), {
            "service.beta.kubernetes.io/aws-load-balancer-healthcheck-interval": "2592000",
        })

    def test_hostname_renders_as_a_string(self):
        for documents in (render(values={"clickhouse": {"hostname": 2}}), render("clickhouse.hostname=2")):
            self.assertEqual(service_annotations(documents),
                             {"external-dns.alpha.kubernetes.io/hostname": "2"})


class NodeSelectorTests(unittest.TestCase):
    EXPECTED = {"dedicated": "true", "pool-generation": "2"}

    def test_values_file_numbers_and_bools_render_as_strings(self):
        documents = render(values={component: {"nodeSelector": {"dedicated": True, "pool-generation": 2}}
                                   for component in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["nodeSelector"], self.EXPECTED, kind)

    def test_values_file_large_numbers_render_without_exponent(self):
        # Values-file numbers arrive as float64, and 1e6 and up is where %v switches
        # to exponent form ("2.592e+06").
        documents = render(values={component: {"nodeSelector": {"gen": 2592000}} for component in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["nodeSelector"]["gen"], "2592000", kind)

    def test_set_numbers_and_bools_render_as_strings(self):
        documents = render(*[f"{component}.nodeSelector.{key}={value}"
                             for component in COMPONENTS
                             for key, value in (("dedicated", "true"), ("pool-generation", "2"))])
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["nodeSelector"], self.EXPECTED, kind)


class TolerationTests(unittest.TestCase):
    def test_values_file_value_and_seconds_render_as_the_operators_types(self):
        tolerations = [
            {"key": "dedicated", "operator": "Equal", "value": True, "effect": "NoExecute",
             "tolerationSeconds": "300"},
            {"key": "pool-generation", "operator": "Equal", "value": 2, "effect": "NoSchedule"},
        ]
        documents = render(values={component: {"tolerations": tolerations} for component in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["tolerations"], [
                {"key": "dedicated", "operator": "Equal", "value": "true", "effect": "NoExecute",
                 "tolerationSeconds": 300},
                {"key": "pool-generation", "operator": "Equal", "value": "2", "effect": "NoSchedule"},
            ], kind)

    def test_values_file_unquoted_seconds_render_as_an_integer(self):
        # YAML gives a bare 300 to Helm as a float64, not a string.
        documents = render(values={component: {"tolerations": [
            {"key": "dedicated", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": 300},
        ]} for component in COMPONENTS})
        for kind in COMPONENTS.values():
            seconds = pod_spec(documents, kind)["tolerations"][0]["tolerationSeconds"]
            self.assertIs(type(seconds), int, kind)
            self.assertEqual(seconds, 300, kind)

    def test_values_file_large_numbers_render_without_exponent(self):
        # Values-file numbers arrive as float64, and 1e6 and up is where %v switches
        # to exponent form ("2.592e+06"), which is neither an integer nor the
        # digits the user wrote. 2592000 is 30 days, a common NoExecute grace period.
        tolerations = [{"key": "dedicated", "operator": "Equal", "value": 2592000, "effect": "NoExecute",
                        "tolerationSeconds": 2592000}]
        documents = render(values={component: {"tolerations": tolerations} for component in COMPONENTS})
        for kind in COMPONENTS.values():
            toleration = pod_spec(documents, kind)["tolerations"][0]
            self.assertEqual(toleration["value"], "2592000", kind)
            self.assertIs(type(toleration["tolerationSeconds"]), int, kind)
            self.assertEqual(toleration["tolerationSeconds"], 2592000, kind)

    def test_zero_padded_seconds_parse_as_decimal(self):
        # int64 parses base 0, so "0300" would read as octal 192 and "08" would
        # read as 0, which evicts immediately on a NoExecute taint.
        for component, kind in COMPONENTS.items():
            for text, expected in (("0300", 300), ("08", 8)):
                with self.subTest(kind=kind, seconds=text):
                    documents = render(values={component: {"tolerations": [
                        {"key": "dedicated", "operator": "Exists", "effect": "NoExecute",
                         "tolerationSeconds": text},
                    ]}})
                    seconds = pod_spec(documents, kind)["tolerations"][0]["tolerationSeconds"]
                    self.assertIs(type(seconds), int)
                    self.assertEqual(seconds, expected)

    def test_set_value_renders_as_a_string(self):
        documents = render(*[f"{component}.tolerations[0].{key}={value}"
                             for component in COMPONENTS
                             for key, value in (("key", "dedicated"), ("operator", "Equal"),
                                                ("value", "true"), ("effect", "NoSchedule"))])
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["tolerations"], [
                {"key": "dedicated", "operator": "Equal", "value": "true", "effect": "NoSchedule"},
            ], kind)

    def test_absent_and_null_fields_stay_absent_and_null(self):
        # An Exists toleration must not gain a value, and a null must not become
        # the string "<nil>".
        tolerations = [
            {"key": "dedicated", "operator": "Exists", "effect": "NoSchedule"},
            {"key": "pool", "operator": "Exists", "value": None, "tolerationSeconds": None},
        ]
        documents = render(values={component: {"tolerations": tolerations} for component in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(pod_spec(documents, kind)["tolerations"], tolerations, kind)

    def test_non_integer_seconds_fail_the_render(self):
        # Converting "3OO" would give 0, which evicts immediately on a NoExecute taint.
        for component in COMPONENTS:
            with self.subTest(component=component):
                with self.assertRaisesRegex(AssertionError, 'tolerationSeconds must be an integer, got "3OO"'):
                    render(values={component: {"tolerations": [
                        {"key": "dedicated", "operator": "Exists", "effect": "NoExecute", "tolerationSeconds": "3OO"},
                    ]}})


class ScalarTests(unittest.TestCase):
    def test_storage_class_renders_as_a_string(self):
        # A quoted "2" in a values file used to render as the integer 2 too.
        for label, documents in (
            ("values file int", render(values={c: {"storageClass": 2} for c in COMPONENTS})),
            ("values file quoted", render(values={c: {"storageClass": "2"} for c in COMPONENTS})),
            ("--set", render(*[f"{c}.storageClass=2" for c in COMPONENTS])),
        ):
            for kind in COMPONENTS.values():
                self.assertEqual(storage_class(documents, kind), "2", f"{label}, {kind}")

    def test_image_renders_as_a_string(self):
        documents = render(values={c: {"image": "true"} for c in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(container_image(documents, kind), "true", kind)

    def test_long_values_stay_on_one_line(self):
        # toYaml folds plain strings longer than 80 characters at spaces; a folded
        # line would break the indentation of the field it is printed into.
        long_value = " ".join(["segment"] * 15)
        documents = render(values={c: {"storageClass": long_value} for c in COMPONENTS})
        for kind in COMPONENTS.values():
            self.assertEqual(storage_class(documents, kind), long_value, kind)


if __name__ == "__main__":
    unittest.main()
