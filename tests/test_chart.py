import subprocess
import unittest
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm/inference"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


class ChartTests(unittest.TestCase):
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
        self.assertIn("GITHUB_RUN_NUMBER", version_step["run"])
        self.assertIn("GITHUB_RUN_ATTEMPT", version_step["run"])
        self.assertIn("0.0.0-sha.${GITHUB_SHA}", version_step["run"])
        self.assertIn("MAJOR.MINOR.PATCH", version_step["run"])
        package_step = next(
            step for step in publish_job["steps"]
            if step.get("name") == "Package and publish versioned inference charts")
        self.assertIn("org.opencontainers.image.revision", package_step["run"])
        self.assertIn("Refusing to overwrite existing released chart version", package_step["run"])
        self.assertIn('helm package "$chart_source" --version "$version"', package_step["run"])
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], "0.0.0")
        self.assertEqual(chart["annotations"]["org.opencontainers.image.revision"], "")

    def render(self, namespace=True, pv=False, extra=()):
        result = subprocess.run(
            [
                "helm", "template", "inference", str(CHART),
                "--namespace", "inference",
                "--set", "nvidia-device-plugin.enabled=false",
                "--set", f"namespaces.create={str(namespace).lower()}",
                "--set", f"inference.pv.create={str(pv).lower()}",
                *extra,
            ],
            check=True, capture_output=True, text=True,
        )
        return [resource for resource in yaml.safe_load_all(result.stdout) if resource]

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
            {"ConfigMap", "Deployment", "HorizontalPodAutoscaler",
             "Service", "PersistentVolumeClaim"},
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
        deployments = [resource for resource in resources if resource["kind"] == "Deployment"]
        self.assertEqual(len(deployments), 1)
        for deployment in deployments:
            name = deployment["metadata"]["name"]
            pod = deployment["spec"]["template"]["spec"]
            volume = next(volume for volume in pod["volumes"] if "persistentVolumeClaim" in volume)
            self.assertEqual(volume["persistentVolumeClaim"]["claimName"], "inference-models")
            container = pod["containers"][0]
            self.assertIn({"name": "LLAMA_CACHE", "value": f"/models/llama.cpp/{name}"}, container["env"])
            self.assertIn({"name": f"{name}-models", "mountPath": "/models"}, container["volumeMounts"])
            self.assertNotIn("imagePullSecrets", pod)

    def test_ingress_uses_alternative_environment_values(self):
        resources = self.render(extra=(
            "--set", "namespace=alternate-inference",
            "--set", "inference.ingress.enabled=true",
            "--set", "inference.ingress.host=models.example.test",
            "--set", "inference.ingress.path=/api",
            "--set", "inference.ingress.className=example-controller",
        ))
        ingress = next(resource for resource in resources if resource["kind"] == "Ingress")
        self.assertEqual(ingress["metadata"]["namespace"], "alternate-inference")
        self.assertNotIn("annotations", ingress["metadata"])
        self.assertNotIn("tls", ingress["spec"])
        self.assertEqual(ingress["spec"]["ingressClassName"], "example-controller")
        self.assertEqual(ingress["spec"]["rules"][0]["host"], "models.example.test")
        self.assertEqual(ingress["spec"]["rules"][0]["http"]["paths"][0]["path"], "/api")

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
            "inference.ingress.enabled=true",
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