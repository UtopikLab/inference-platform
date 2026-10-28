import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
CHART = ROOT / "helm/inference"
WORKFLOW = ROOT / ".github/workflows/ci.yml"


@pytest.mark.parametrize("report_exists", [False, True])
def test_ci_workflow_reporting(tmp_path, report_exists):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    assert workflow["permissions"] == {"contents": "read"}
    assert jobs["lint"]["name"] == "Lint Python"
    assert jobs["test"]["name"] == "test-${{ matrix.slice }}"
    assert jobs["chart-validation"]["name"] == "Validate Helm chart"
    assert jobs["publish-chart"]["needs"] == ["lint", "test", "chart-validation"]
    assert "HELM_CHART_PUBLISH_ENABLED == 'true'" in jobs["publish-chart"]["if"]
    assert set(jobs["summary"]["needs"]) == set(jobs) - {"summary"}
    assert jobs["summary"]["if"] == "always()"
    for job in jobs.values():
        for step in job["steps"]:
            assert step.get("name")
            if "run" not in step:
                continue
            script = step["run"]
            if step.get("shell") == "python":
                compile(script, "<workflow>", "exec")
            else:
                subprocess.run(
                    ["bash", "-n"], input=re.sub(r"\$\{\{.*?\}\}", "ci-value", script),
                    text=True, check=True, capture_output=True,
                )
    steps = {step["name"]: step for step in jobs["test"]["steps"]}
    assert steps["Run tests"]["id"] == "pytest"
    assert steps["Run tests"]["run"] == (
        'mkdir -p test-results\npytest "$TEST_PATH" -v -ra --durations=10 --junitxml=test-results/pytest.xml\n'
    )
    assert steps["Summarize test results"]["if"] == "always()"
    upload = steps["Upload test results"]
    assert upload["if"] == "always() && hashFiles('test-results/pytest.xml') != ''"
    assert upload["with"] == {
        "name": "${{ format('python-test-results-{0}', matrix.slice) }}",
        "path": "test-results/pytest.xml", "retention-days": "14",
    }
    assert steps["Summarize test results"]["env"]["TEST_ARTIFACT"] == upload["with"]["name"]
    if report_exists:
        report = tmp_path / "test-results/pytest.xml"
        report.parent.mkdir()
        report.write_text(
            '<testsuites><testsuite tests="3" failures="1" errors="0" skipped="1" time="1.5">'
            '<testcase classname="module" name="passes" time="0.5"/>'
            '<testcase classname="module" name="fails" time="0.5">'
            '<failure message="bad | &lt;value&gt;"/></testcase>'
            '<testcase classname="module" name="skips" time="0.5">'
            '<skipped message="optional"/></testcase></testsuite></testsuites>',
            encoding="utf-8",
        )
    summary = tmp_path / "summary.md"
    slices = jobs["test"]["strategy"]["matrix"]["slice"]
    environment = {
        **os.environ, "GITHUB_STEP_SUMMARY": str(summary), "TEST_OUTCOME": "failure",
        "TEST_ARTIFACT": f"python-test-results-{slices[0]}",
    }
    subprocess.run(
        [sys.executable, "-c", steps["Summarize test results"]["run"]],
        cwd=tmp_path, env=environment, check=True,
    )
    text = summary.read_text()
    if report_exists:
        assert "| 3 | 1 | 1 | 0 | 1 | 1.50s |" in text
        assert "bad &#124; &lt;value&gt;" in text
        assert "**python-test-results-shared** artifact" in text
    else:
        assert "No test report was produced" in text
    environment.update({
        "JOB_RESULTS": json.dumps({"test": {"result": "failure"}, "publish-chart": {"result": "skipped"}}),
        "SOURCE_COMMIT": "a" * 40, "GITHUB_SERVER_URL": "https://github.com",
        "GITHUB_REPOSITORY": "owner/product", "GITHUB_RUN_ID": "123",
    })
    subprocess.run(
        [sys.executable, "-c", jobs["summary"]["steps"][0]["run"]],
        cwd=tmp_path, env=environment, check=True,
    )
    text = summary.read_text()
    assert "| test | failure |" in text
    assert "| publish-chart | skipped |" in text
    assert "https://github.com/owner/product/actions/runs/123" in text


def test_ci_workflow_matrix_partition_and_dependencies(tmp_path):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    test_job = workflow["jobs"]["test"]
    assert test_job["strategy"] == {
        "fail-fast": "false", "matrix": {"slice": ["shared"]},
    }
    defaults = {key: value for key, value in workflow["env"].items() if key.startswith("TEST_")}
    assert defaults == {
        "TEST_REQUIREMENTS_FORMAT": "requirements-test.txt",
        "TEST_PATH_FORMAT": "tests",
    }
    effective = {key.removesuffix("_FORMAT"): value.format("shared") for key, value in defaults.items()}
    assert (ROOT / effective["TEST_REQUIREMENTS"]).read_text().splitlines() == ["pytest", "PyYAML>=6,<7"]
    assert "env" not in test_job
    steps = {step["name"]: step for step in test_job["steps"]}
    assert steps["Set up Helm"]["uses"] == "azure/setup-helm@v4"
    assert steps["Install dependencies"]["env"] == {
        "TEST_REQUIREMENTS": "${{ format(env.TEST_REQUIREMENTS_FORMAT, matrix.slice) }}",
    }
    assert steps["Run tests"]["env"] == {
        "TEST_PATH": "${{ format(env.TEST_PATH_FORMAT, matrix.slice) }}",
    }
    assert steps["Install dependencies"]["run"] == (
        'python -m pip install --upgrade pip\npython -m pip install -r "$TEST_REQUIREMENTS"\n'
    )
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands.jsonl"
    for command in ("python", "pytest"):
        executable = binaries / command
        executable.write_text(
            f"#!{sys.executable}\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
            "    log.write(json.dumps([Path(sys.argv[0]).name, *sys.argv[1:]]) + '\\n')\n",
            encoding="utf-8",
        )
        executable.chmod(0o755)
    environment = {
        **os.environ, "PATH": f"{binaries}:{os.environ['PATH']}", "COMMAND_LOG": str(log),
        **effective,
    }
    for step in ("Install dependencies", "Run tests"):
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", steps[step]["run"]],
            cwd=tmp_path, env=environment, check=True, capture_output=True, text=True,
        )
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands == [
        ["python", "-m", "pip", "install", "--upgrade", "pip"],
        ["python", "-m", "pip", "install", "-r", "requirements-test.txt"],
        ["pytest", "tests", "-v", "-ra", "--durations=10", "--junitxml=test-results/pytest.xml"],
    ]
    environment.update({
        "TEST_REQUIREMENTS": "requirements with spaces $(touch injected).txt",
        "TEST_PATH": "tests with spaces",
    })
    for step in ("Install dependencies", "Run tests"):
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", steps[step]["run"]],
            cwd=tmp_path, env=environment, check=True, capture_output=True, text=True,
        )
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands[-2] == [
        "python", "-m", "pip", "install", "-r", "requirements with spaces $(touch injected).txt",
    ]
    assert commands[-1][1:-4] == ["tests with spaces"]
    assert not (tmp_path / "injected").exists()


def test_ci_workflow_helm_configuration(tmp_path):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    jobs = workflow["jobs"]
    prepare = [next(step["run"] for step in jobs[job]["steps"]
                    if step["name"] == "Prepare Helm dependencies")
               for job in ("test", "chart-validation", "publish-chart")]
    assert len(set(prepare)) == 1
    binaries = tmp_path / "bin"
    binaries.mkdir()
    log = tmp_path / "commands.jsonl"
    executable = binaries / "helm"
    executable.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "with open(os.environ['COMMAND_LOG'], 'a') as log:\n"
        "    log.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    executable.chmod(0o755)
    environment = {
        **os.environ, **workflow["env"], "PATH": f"{binaries}:{os.environ['PATH']}",
        "COMMAND_LOG": str(log),
    }
    script = next(step["run"] for step in jobs["chart-validation"]["steps"]
                  if step["name"] == "Lint and render Helm chart")
    for command in (prepare[0], script):
        subprocess.run(
            ["bash", "-e", "-o", "pipefail", "-c", command],
            cwd=tmp_path, env=environment, check=True, capture_output=True, text=True,
        )
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    assert commands == [
        ["repo", "add", "nvidia", "https://nvidia.github.io/k8s-device-plugin"],
        ["dependency", "build", "helm/inference"],
        ["lint", "helm/inference", "--strict"],
        ["template", "inference", "helm/inference", "--namespace", "inference"],
    ]


@pytest.mark.parametrize("event", ["push", "workflow_dispatch", "pull_request", "release"])
@pytest.mark.parametrize("ref", ["refs/heads/main", "refs/tags/v1.2.3", "refs/heads/other", "refs/pull/1/merge"])
@pytest.mark.parametrize("enabled", ["true", "false", "TRUE"])
@pytest.mark.parametrize("validated", [True, False])
def test_chart_publish_gate(event, ref, enabled, validated):
    job = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["jobs"]["publish-chart"]
    expression = " ".join(job["if"].removeprefix("${{").removesuffix("}}").split())
    assert expression == (
        "success() && (github.event_name == 'push' || github.event_name == 'workflow_dispatch') && "
        "(github.ref == 'refs/heads/main' || startsWith(github.ref, 'refs/tags/')) && "
        "vars.HELM_CHART_PUBLISH_ENABLED == 'true'"
    )
    expression = expression.replace("startsWith(github.ref, 'refs/tags/')", str(ref.startswith("refs/tags/")))
    for key, value in {
        "success()": validated, "github.event_name": event, "github.ref": ref,
        "vars.HELM_CHART_PUBLISH_ENABLED": enabled,
    }.items():
        expression = expression.replace(key, repr(value))
    actual = eval(expression.replace("&&", "and").replace("||", "or"), {"__builtins__": {}})
    assert actual == (validated and event in ("push", "workflow_dispatch")
                      and (ref == "refs/heads/main" or ref.startswith("refs/tags/")) and enabled == "true")
    assert job["needs"] == ["lint", "test", "chart-validation"]


@pytest.mark.parametrize("ref,version", [
    ("refs/heads/main", "0.0.0-dev"),
    ("refs/tags/v1.2.3", "1.2.3"), ("refs/tags/1.2.3", "1.2.3"),
    ("refs/tags/v0.0.0", "0.0.0"), ("refs/tags/10.20.30", "10.20.30"),
    ("refs/tags/v0.0.0-dev", "0.0.0-dev"), ("refs/tags/1.2.3-rc.1", "1.2.3-rc.1"),
    *[("refs/tags/" + tag, None) for tag in (
        "v01.2.3", "1.02.3", "1.2.03", "v1.2", "vv1.2.3",
        "v1.2.3-", "v1.2.3-.rc", "v1.2.3-rc.", "v1.2.3-rc..1", "v1.2.3-rc.01",
        "v1.2.3-01", "v1.2.3-rc_1",
        "1.2.3+build", "dev", "1.2.3/other", "1.2.3\ninjected", "",
    )],
    ("refs/heads/other", None), ("refs/pull/1/merge", None),
])
def test_chart_version_selection(tmp_path, ref, version):
    job = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)["jobs"]["publish-chart"]
    selector = job["steps"][0]
    assert selector["id"] == "chart-version"
    output = tmp_path / "output"
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", selector["run"]], check=False, capture_output=True, text=True,
        env={**os.environ, "GITHUB_REF": ref, "GITHUB_SHA": "a" * 40, "GITHUB_OUTPUT": str(output)},
    )
    assert (result.returncode == 0) == (version is not None), result.stderr
    if version is None:
        assert not output.exists()
    else:
        assert dict(line.split("=", 1) for line in output.read_text().splitlines()) == {
            "package_version": version, "source_version": "0.0.0-sha." + "a" * 40,
        }


@pytest.mark.parametrize("tagged", [False, True])
@pytest.mark.parametrize("scenario", [
    "fresh", "same", "mismatch", "release-exists", "pull-error", "bad-digest", "bad-revision", "digest-mismatch",
])
def test_chart_publisher_versions_and_verification(tmp_path, tagged, scenario):
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    job = workflow["jobs"]["publish-chart"]
    steps = {step["name"]: step for step in job["steps"]}
    binaries = tmp_path / "bin"
    binaries.mkdir()
    stub = binaries / "publication-stub"
    stub.write_text(f"#!{sys.executable}\n" + '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
command = Path(sys.argv[0]).name
with open(os.environ["COMMAND_LOG"], "a") as log:
    log.write(json.dumps([command, *args]) + "\\n")
scenario = os.environ["SCENARIO"]
digest = "sha256:" + "b" * 64
if command == "oras":
    if args[0] == "resolve":
        print("sha256:" + "c" * 64 if scenario == "digest-mismatch" else digest)
    sys.exit(0)
if args[0] == "pull":
    version = args[args.index("--version") + 1]
    destination = Path(args[args.index("--destination") + 1])
    source = version.startswith("0.0.0-sha.")
    if scenario == "pull-error":
        sys.exit("unauthorized")
    exists = (Path(os.environ["RUNNER_TEMP"]) / ("pushed-" + version)).exists()
    if not exists and not (source and scenario in ("same", "mismatch")) and not (not source and scenario == "release-exists"):
        sys.exit("manifest unknown: 404 not found")
    (destination / (os.environ["CHART_NAME"] + "-" + version + ".tgz")).touch()
    print("Digest: " + ("invalid" if scenario == "bad-digest" else digest), file=sys.stderr)
elif args[:2] == ["show", "chart"]:
    revision = os.environ["SOURCE_COMMIT"]
    if scenario == "mismatch" or (scenario == "bad-revision" and "source-chart/" in args[2]):
        revision = "c" * 40
    print("annotations:\\n  org.opencontainers.image.revision: " + revision)
elif args[0] == "package":
    version = args[args.index("--version") + 1]
    destination = Path(args[args.index("--destination") + 1])
    (destination / (os.environ["CHART_NAME"] + "-" + version + ".tgz")).touch()
elif args[0] == "push":
    version = Path(args[1]).name.removeprefix(os.environ["CHART_NAME"] + "-").removesuffix(".tgz")
    (Path(os.environ["RUNNER_TEMP"]) / ("pushed-" + version)).touch()
else:
    sys.exit("Unexpected publication command")
''', encoding="utf-8")
    stub.chmod(0o755)
    for command in ("helm", "oras"):
        (binaries / command).symlink_to(stub)
    source_version = "0.0.0-sha." + "a" * 40
    output = tmp_path / "output"
    log = tmp_path / "commands.jsonl"
    environment = {
        **os.environ, **workflow["env"], **job["env"],
        "CHART_PATH": str(CHART), "RUNNER_TEMP": str(tmp_path), "SCENARIO": scenario,
        "COMMAND_LOG": str(log), "PATH": f"{Path(sys.executable).parent}:{binaries}:{os.environ['PATH']}",
        "GITHUB_REF": "refs/tags/v1.2.3" if tagged else "refs/heads/main",
        "SOURCE_COMMIT": "a" * 40, "SOURCE_CHART_VERSION": source_version,
        "PACKAGE_VERSION": "1.2.3" if tagged else "0.0.0-dev",
        "GITHUB_OUTPUT": str(output), "GITHUB_STEP_SUMMARY": str(tmp_path / "summary.md"),
    }
    result = subprocess.run(
        ["bash", "-euo", "pipefail", "-c", steps["Package and publish chart"]["run"]],
        env=environment, check=False, capture_output=True, text=True,
    )
    succeeds = scenario in ("fresh", "same") or (scenario == "release-exists" and not tagged)
    assert (result.returncode == 0) == succeeds, result.stdout + result.stderr
    commands = [json.loads(line) for line in log.read_text().splitlines()]
    pushes = [command for command in commands if command[:2] == ["helm", "push"]]
    aliases = [command for command in commands if command[:2] == ["oras", "tag"]]
    assert len(aliases) == int(succeeds and not tagged)
    if aliases:
        assert aliases[0] == ["oras", "tag", f"{job['env']['CHART_OCI_REPOSITORY']}/{job['env']['CHART_NAME']}@sha256:" + "b" * 64, "dev"]
    if scenario in ("pull-error", "mismatch") and not tagged or scenario == "release-exists" and tagged:
        assert not pushes
    if not succeeds:
        assert not output.exists()
        assert not (tmp_path / "summary.md").exists()
        assert not any(Path(command[2]).name == "inference-0.0.0-dev.tgz" for command in pushes)
        return
    assert len(pushes) == 1 + int(scenario != "same")
    expected_versions = (["1.2.3"] if tagged else []) + ([source_version] if scenario != "same" else [])
    if not tagged:
        expected_versions.append("0.0.0-dev")
    assert [Path(command[2]).name for command in pushes] == [
        f"{job['env']['CHART_NAME']}-{version}.tgz" for version in expected_versions
    ]
    assert all(command[3] == f"oci://{job['env']['CHART_OCI_REPOSITORY']}" for command in pushes)
    assert yaml.safe_load((tmp_path / "chart-source/Chart.yaml").read_text())["annotations"][
        "org.opencontainers.image.revision"
    ] == "a" * 40
    assert not output.exists()
    summary = (tmp_path / "summary.md").read_text()
    publication_kind = "immutable release" if tagged else "mutable development"
    assert f"Published {publication_kind} chart version: `{environment['PACKAGE_VERSION']}`" in summary
    assert ("Updated OCI alias: `dev`" in summary) == (not tagged)
    assert f"Source chart version: `{source_version}`" in summary
    assert f"Source digest: `sha256:{'b' * 64}`" in summary


def test_ci_has_no_release_manifest_pipeline():
    workflow = yaml.load(WORKFLOW.read_text(), Loader=yaml.BaseLoader)
    assert "release-manifest" not in workflow["jobs"]
    publish_chart = workflow["jobs"]["publish-chart"]
    assert "outputs" not in publish_chart
    for step in publish_chart["steps"]:
        assert "manifest" not in step["name"].lower()
        assert not step.get("uses", "").startswith(("actions/upload-artifact@", "actions/download-artifact@"))
        assert "id" not in step or step["id"] == "chart-version"
    for obsolete in ("inference-platform-release-", "inference-release-manifest.json", "chart_digest:"):
        assert obsolete not in WORKFLOW.read_text()


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
        self.assertIn('${chart_version#v}', version_step["run"])
        self.assertIn('${GITHUB_REF#refs/tags/}', version_step["run"])
        self.assertIn("0.0.0-sha.${GITHUB_SHA}", version_step["run"])
        self.assertIn("MAJOR.MINOR.PATCH", version_step["run"])
        self.assertIn('echo "package_version=$chart_version"', version_step["run"])
        package_step = next(
            step for step in publish_job["steps"]
            if step["name"] == "Package and publish chart")
        self.assertIn("org.opencontainers.image.revision", package_step["run"])
        self.assertIn("Refusing to overwrite existing released chart version", package_step["run"])
        self.assertIn('publish_chart "$SOURCE_CHART_VERSION" source', package_step["run"])
        self.assertIn('helm package "$chart_source" --version "$package_version"', package_step["run"])
        self.assertIn('oras tag "$CHART_OCI_REPOSITORY/$CHART_NAME@$chart_digest" "$CHART_ALIAS"', package_step["run"])
        self.assertIn("Set up ORAS", [step.get("name") for step in publish_job["steps"]])
        login_step = next(
            step for step in publish_job["steps"]
            if step.get("name") == "Log in to GHCR")
        self.assertIn(
            'oras login ghcr.io --username "$GITHUB_ACTOR" --password-stdin',
            login_step["run"],
        )
        chart = yaml.safe_load((CHART / "Chart.yaml").read_text())
        self.assertEqual(chart["version"], "0.0.0")
        self.assertEqual(chart["annotations"]["org.opencontainers.image.revision"], "")
        self.assertIn('chart_version="0.0.0-dev"', version_step["run"])

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
            self.assertTrue(rendered["spec"]["selector"]["matchLabels"].items() <=
                            rendered["spec"]["template"]["metadata"]["labels"].items())
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
        resources = self.render(pv=True, release_namespace="tenant-models", extra=(
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
        self.assertEqual(volume["spec"]["claimRef"], {
            "namespace": "tenant-models", "name": "alternate-model-claim",
        })
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
        self.assertEqual(volume["spec"]["claimRef"], {
            "namespace": "inference", "name": claim["metadata"]["name"],
        })

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