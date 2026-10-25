import subprocess
import tempfile
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm/inference"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


class ChartTests(unittest.TestCase):
    def test_namespace_is_not_a_chart_value(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        schema = yaml.safe_load((CHART / "values.schema.json").read_text())
        self.assertNotIn("namespace", values)
        self.assertNotIn("namespace", schema["properties"])

    def test_chart_publication_uses_release_tag_and_commit_addressed_versions(self):
        workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
        self.assertEqual(workflow["on"]["push"]["tags"], ["v*", "[0-9]*"])
        publish_job = workflow["jobs"]["publish-chart"]
        self.assertIn("refs/tags/", publish_job["if"])
        self.assertIn("refs/heads/main", publish_job["if"])
        self.assertIn("Install chart metadata tool", [
            step.get("name") for step in publish_job["steps"]
        ])
        version_step = next(
            step for step in publish_job["steps"] if step.get("id") == "chart-version")
        self.assertIn('${GITHUB_REF_NAME#v}', version_step["run"])
        self.assertEqual(version_step["env"]["GITHUB_RUN_NUMBER"], "${{ github.run_number }}")
        self.assertEqual(version_step["env"]["GITHUB_RUN_ATTEMPT"], "${{ github.run_attempt }}")
        self.assertIn("0.0.0-sha.${GITHUB_SHA}", version_step["run"])
        self.assertIn("MAJOR.MINOR.PATCH", version_step["run"])
        package_step = next(
            step for step in publish_job["steps"]
            if step.get("name") == "Package and publish versioned inference charts")
        self.assertIn("org.opencontainers.image.revision", package_step["run"])
        self.assertIn("Refusing to overwrite existing released chart version", package_step["run"])
        self.assertIn('if [[ "$kind" == source ]]', package_step["run"])
        self.assertIn("Refreshing mutable chart version $version", package_step["run"])
        self.assertIn('helm package "$chart_source" --version "$version"', package_step["run"])
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], "0.0.0")
        self.assertEqual(chart["annotations"]["org.opencontainers.image.revision"], "")

    def render(self, namespace=True, pv=False, extra=(), release_namespace="inference", values=None):
        command = [
                "helm", "template", "inference", str(CHART),
                "--namespace", release_namespace,
                "--set", "nvidia-device-plugin.enabled=false",
                "--set", f"namespaces.create={str(namespace).lower()}",
                "--set", f"inference.pv.create={str(pv).lower()}",
                *extra,
            ]
        if values is None:
            result = subprocess.run(command, check=True, capture_output=True, text=True)
        else:
            with tempfile.TemporaryDirectory() as directory:
                values_path = Path(directory) / "values.yaml"
                values_path.write_text(yaml.safe_dump(values), encoding="utf-8")
                result = subprocess.run(
                    [*command[:4], "--values", str(values_path), *command[4:]],
                    check=True, capture_output=True, text=True,
                )
        return [resource for resource in yaml.safe_load_all(result.stdout) if resource]

    def test_namespace_resource_uses_release_namespace(self):
        resources = self.render(release_namespace="tenant-models", extra=(
            "--set", "inference.ingress.enabled=true",
            "--set", "inference.ingress.routes[0].host=models.example.test",
            "--set", "inference.ingress.routes[0].path=/v1",
            "--set", "inference.ingress.routes[0].profile=coder",
        ))
        namespace = next(resource for resource in resources if resource["kind"] == "Namespace")
        self.assertEqual(namespace["metadata"]["name"], "tenant-models")
        for resource in resources:
            if resource["kind"] != "Namespace":
                self.assertEqual(resource["metadata"]["namespace"], "tenant-models")

    def test_device_plugin_uses_release_namespace_and_rejects_conflicting_override(self):
        resources = self.render(release_namespace="tenant-models", extra=(
            "--set", "nvidia-device-plugin.enabled=true",
        ))
        for resource in resources:
            if "namespace" in resource["metadata"]:
                self.assertEqual(resource["metadata"]["namespace"], "tenant-models")
            for subject in resource.get("subjects", []):
                if subject["kind"] == "ServiceAccount":
                    self.assertEqual(subject["namespace"], "tenant-models")
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.render(release_namespace="tenant-models", extra=(
                "--set", "nvidia-device-plugin.enabled=true",
                "--set", "nvidia-device-plugin.namespaceOverride=other-models",
            ))
        self.assertIn("must match the release namespace", error.exception.stderr)

    def test_namespace_and_pv_switches_are_independent(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        self.assertTrue(values["namespaces"]["create"])
        self.assertFalse(values["inference"]["pv"]["create"])
        self.assertTrue(values["nvidia-device-plugin"]["enabled"])
        for namespace in (False, True):
            for pv in (False, True):
                with self.subTest(namespace=namespace, pv=pv):
                    resources = self.render(namespace, pv, extra=(
                        "--set", "inference.pv.nodeName=worker-b",
                        "--set", "inference.pv.path=/srv/model-cache",
                    ))
                    kinds = [resource["kind"] for resource in resources]
                    self.assertEqual(kinds.count("Namespace"), int(namespace))
                    self.assertEqual(kinds.count("PersistentVolume"), int(pv))
                    self.assertEqual(kinds.count("PersistentVolumeClaim"), 1)

    def test_routine_render_is_namespaced_and_preserves_storage_and_cache(self):
        resources = self.render(namespace=False, pv=False)
        self.assertEqual(
            {resource["kind"] for resource in resources},
            {"Deployment", "HorizontalPodAutoscaler", "Service", "PersistentVolumeClaim"},
        )
        for resource in resources:
            self.assertEqual(resource["metadata"]["namespace"], "inference")
            self.assertNotIn("hostPath:", yaml.safe_dump(resource))
        claim = next(resource for resource in resources if resource["kind"] == "PersistentVolumeClaim")
        self.assertEqual(claim["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(claim["metadata"]["name"], "inference-models")
        self.assertNotIn("volumeName", claim["spec"])
        self.assertNotIn("storageClassName", claim["spec"])
        self.assertEqual(claim["spec"]["resources"]["requests"]["storage"], "1Gi")
        deployments = {resource["metadata"]["name"]: resource for resource in resources
                       if resource["kind"] == "Deployment"}
        self.assertEqual(set(deployments), {"coder", "judge"})
        self.assertEqual(deployments["coder"]["spec"]["replicas"], 2)
        self.assertEqual(deployments["judge"]["spec"]["replicas"], 1)
        services = {resource["metadata"]["name"]: resource for resource in resources
                    if resource["kind"] == "Service"}
        hpas = {resource["metadata"]["name"]: resource for resource in resources
                if resource["kind"] == "HorizontalPodAutoscaler"}
        self.assertEqual(set(services), set(deployments))
        self.assertEqual(set(hpas), set(deployments))
        for name, rendered in deployments.items():
            pod = rendered["spec"]["template"]["spec"]
            volume = next(volume for volume in pod["volumes"] if "persistentVolumeClaim" in volume)
            self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "inference-models")
            container = pod["containers"][0]
            self.assertIn({"name": "LLAMA_CACHE", "value": f"/models/llama.cpp/{name}"}, container["env"])
            self.assertIn({"name": "models", "mountPath": "/models"}, container["volumeMounts"])
            self.assertIn("--ctx-size", container["args"])
            service_port = services[name]["spec"]["ports"][0]
            self.assertEqual(container["ports"][0]["containerPort"], service_port["port"])
            self.assertEqual(service_port["targetPort"], container["ports"][0]["name"])
            self.assertEqual(services[name]["spec"]["selector"],
                             rendered["spec"]["selector"]["matchLabels"])
            self.assertEqual(hpas[name]["spec"]["minReplicas"], rendered["spec"]["replicas"])
            self.assertEqual(hpas[name]["spec"]["maxReplicas"], rendered["spec"]["replicas"])
            self.assertNotIn("imagePullSecrets", pod)

    def test_profile_ports_drive_listener_service_probes_and_ingress_backend(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        values["inference"]["servers"][0]["service"]["port"] = 8100
        values["inference"]["servers"][0]["modelArgs"] = ["--model", "/models/test.gguf"]
        resources = self.render(values=values, extra=(
            "--set", "inference.ingress.enabled=true",
            "--set", "inference.ingress.routes[0].host=models.example.test",
            "--set", "inference.ingress.routes[0].path=/coder",
            "--set", "inference.ingress.routes[0].profile=coder",
            "--set", "inference.ingress.routes[1].host=models.example.test",
            "--set", "inference.ingress.routes[1].path=/judge",
            "--set", "inference.ingress.routes[1].profile=judge",
        ), release_namespace="alternate-inference")
        deployment = next(resource for resource in resources
                          if resource["kind"] == "Deployment" and resource["metadata"]["name"] == "coder")
        container = deployment["spec"]["template"]["spec"]["containers"][0]
        service = next(resource for resource in resources
                        if resource["kind"] == "Service" and resource["metadata"]["name"] == "coder")
        ingress = next(resource for resource in resources if resource["kind"] == "Ingress")
        self.assertIn("8100", container["args"])
        self.assertIn("--model", container["args"])
        self.assertIn("/models/test.gguf", container["args"])
        self.assertEqual(container["ports"][0]["containerPort"], 8100)
        self.assertEqual(container["startupProbe"]["httpGet"]["port"], "http")
        self.assertEqual(service["spec"]["ports"][0]["port"], 8100)
        self.assertEqual(ingress["metadata"]["namespace"], "alternate-inference")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "models.example.test")
        paths = ingress["spec"]["rules"][0]["http"]["paths"]
        self.assertEqual({path["path"] for path in paths}, {"/coder", "/judge"})
        self.assertEqual({path["backend"]["service"]["name"] for path in paths}, {"coder", "judge"})
        self.assertEqual(next(path for path in paths if path["path"] == "/coder")
                         ["backend"]["service"]["port"]["number"], 8100)

    def test_profiles_reject_duplicate_names_and_listener_argument_overrides(self):
        values = yaml.safe_load((CHART / "values.yaml").read_text())
        values["inference"]["servers"].append(dict(values["inference"]["servers"][0]))
        with self.assertRaises(subprocess.CalledProcessError) as duplicate:
            self.render(values=values)
        self.assertIn("profile names must be unique", duplicate.exception.stderr)

        values = yaml.safe_load((CHART / "values.yaml").read_text())
        values["inference"]["servers"][0]["serverArgs"].append("--port")
        with self.assertRaises(subprocess.CalledProcessError) as listener_arg:
            self.render(values=values)
        self.assertIn("listener host/port through service.port", listener_arg.exception.stderr)

    def test_ingress_uses_alternative_environment_values(self):
        resources = self.render(extra=(
            "--set", "inference.ingress.enabled=true",
            "--set", "inference.ingress.routes[0].host=models.example.test",
            "--set", "inference.ingress.routes[0].path=/api",
            "--set", "inference.ingress.routes[0].profile=coder",
            "--set", "inference.ingress.className=example-controller",
        ), release_namespace="alternate-inference")
        ingress = next(resource for resource in resources if resource["kind"] == "Ingress")
        self.assertEqual(ingress["metadata"]["namespace"], "alternate-inference")
        self.assertNotIn("annotations", ingress["metadata"])
        self.assertNotIn("tls", ingress["spec"])
        self.assertEqual(ingress["spec"]["ingressClassName"], "example-controller")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "models.example.test")
        self.assertEqual(ingress["spec"]["rules"][0]["http"]["paths"][0]["path"], "/api")

    def test_ingress_tls_references_preprovisioned_secret_for_configured_host(self):
        resources = self.render(extra=(
            "--set", "inference.ingress.enabled=true",
            "--set", "inference.ingress.routes[0].host=models.example.test",
            "--set", "inference.ingress.routes[0].path=/v1",
            "--set", "inference.ingress.routes[0].profile=coder",
            "--set", "inference.ingress.className=nginx",
            "--set", "inference.ingress.tls[0].secretName=models-tls",
            "--set", "inference.ingress.tls[0].hosts[0]=models.example.test",
        ))
        ingress = next(resource for resource in resources if resource["kind"] == "Ingress")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "models.example.test")
        self.assertEqual(ingress["spec"]["tls"], [{
            "hosts": ["models.example.test"], "secretName": "models-tls",
        }])
        self.assertNotIn("annotations", ingress["metadata"])
        with self.assertRaises(subprocess.CalledProcessError) as error:
            self.render(extra=(
                "--set", "inference.ingress.enabled=true",
                "--set", "inference.ingress.routes[0].host=models.example.test",
                "--set", "inference.ingress.routes[0].path=/v1",
                "--set", "inference.ingress.routes[0].profile=coder",
                "--set", "inference.ingress.tls[0].secretName=models-tls",
                "--set", "inference.ingress.tls[0].hosts[0]=other.example.test",
            ))
        self.assertIn("must match an inference.ingress.routes host", error.exception.stderr)

    def test_ingress_cert_manager_supports_issuer_and_cluster_issuer(self):
        for issuer_kind, annotation in (
            ("Issuer", "cert-manager.io/issuer"),
            ("ClusterIssuer", "cert-manager.io/cluster-issuer"),
        ):
            with self.subTest(issuer_kind=issuer_kind):
                resources = self.render(extra=(
                    "--set", "inference.ingress.enabled=true",
                    "--set", "inference.ingress.routes[0].host=models.example.test",
                    "--set", "inference.ingress.routes[0].path=/v1",
                    "--set", "inference.ingress.routes[0].profile=coder",
                    "--set", "inference.ingress.annotations.owner=platform",
                    "--set", "inference.ingress.certManager.enabled=true",
                    "--set", "inference.ingress.certManager.issuerName=letsencrypt-prod",
                    "--set", f"inference.ingress.certManager.issuerKind={issuer_kind}",
                    "--set", "inference.ingress.tls[0].secretName=models-tls",
                    "--set", "inference.ingress.tls[0].hosts[0]=models.example.test",
                ))
                ingress = next(resource for resource in resources if resource["kind"] == "Ingress")
                self.assertEqual(ingress["metadata"]["annotations"], {
                    "owner": "platform", annotation: "letsencrypt-prod",
                })
                self.assertEqual(ingress["spec"]["rules"][0]["host"],
                                 ingress["spec"]["tls"][0]["hosts"][0])
                self.assertEqual(ingress["spec"]["tls"][0]["secretName"], "models-tls")

    def test_local_pv_uses_alternative_environment_values(self):
        resources = self.render(pv=True, extra=(
            "--set", "inference.pv.nodeName=worker-b",
            "--set", "inference.pv.path=/srv/model-cache",
            "--set", "inference.pv.name=alternate-model-disk",
            "--set", "inference.pvc.name=alternate-model-claim",
            "--set", "inference.pvc.storage=37Gi",
            "--set", "inference.pvc.storageClassName=local-retained",
            "--show-only", "templates/namespace.yaml",
            "--show-only", "templates/pv.yaml",
            "--show-only", "templates/pvc.yaml",
        ))
        self.assertEqual({resource["kind"] for resource in resources},
                         {"Namespace", "PersistentVolume", "PersistentVolumeClaim"})
        volume = next(resource for resource in resources if resource["kind"] == "PersistentVolume")
        claim = next(resource for resource in resources if resource["kind"] == "PersistentVolumeClaim")
        self.assertEqual(volume["metadata"]["name"], "alternate-model-disk")
        self.assertEqual(claim["metadata"]["name"], "alternate-model-claim")
        self.assertEqual(claim["spec"]["volumeName"], "alternate-model-disk")
        self.assertEqual(claim["spec"]["storageClassName"], "local-retained")
        self.assertEqual(volume["spec"]["storageClassName"], "local-retained")
        self.assertNotIn("namespace", volume["metadata"])
        self.assertEqual(volume["metadata"]["annotations"]["helm.sh/resource-policy"], "keep")
        self.assertEqual(volume["spec"]["persistentVolumeReclaimPolicy"], "Retain")
        self.assertEqual(volume["spec"]["capacity"]["storage"], "37Gi")
        self.assertEqual(volume["spec"]["local"]["path"], "/srv/model-cache")
        affinity = volume["spec"]["nodeAffinity"]["required"]["nodeSelectorTerms"][0]
        self.assertEqual(affinity["matchFields"][0]["values"], ["worker-b"])
        rendered = yaml.safe_dump_all(resources)
        for literal in ("k8s-node01", "/mnt/k8s-local-nvme", "500Gi"):
            self.assertNotIn(literal, rendered)

    def test_created_local_pv_and_claim_use_the_same_default_class_and_binding(self):
        resources = self.render(pv=True, extra=(
            "--set", "inference.pv.nodeName=worker-b",
            "--set", "inference.pv.path=/srv/model-cache",
        ))
        volume = next(resource for resource in resources if resource["kind"] == "PersistentVolume")
        claim = next(resource for resource in resources if resource["kind"] == "PersistentVolumeClaim")
        self.assertEqual(volume["spec"]["storageClassName"], "")
        self.assertEqual(claim["spec"]["storageClassName"], "")
        self.assertEqual(claim["spec"]["volumeName"], volume["metadata"]["name"])

    def test_schema_rejects_invalid_ownership_and_storage_values(self):
        for setting in (
            "namespaces.create=disabled", "inference.pv.create=disabled",
            "inference.pvc.name=Invalid_Name", "inference.pvc.storage=not-a-size",
            "inference.pv.create=true", "inference.pv.path=relative/path",
            "inference.ingress.enabled=true", "inference.gpuCount=2",
            "inference.configMaps[0].name=old-profile",
            "inference.servers[0].replicas=0",
        ):
            with self.subTest(setting=setting):
                result = subprocess.run(
                    ["helm", "lint", "--strict", str(CHART), "--set-string", setting],
                    capture_output=True, text=True, check=False,
                )
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("schema", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()