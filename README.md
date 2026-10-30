# inference-platform

Inference deployment source for the Code Factory system. This repository owns
the `inference` Helm chart and its integration with the upstream llama.cpp
runtime; it does not maintain a forked inference image.

- Helm chart: `helm/inference/`
- Application components: the
	[codefactory repository](https://github.com/UtopikPrompt/codefactory)
- Environment selection and deployment operations: the
	[infra-codefactory repository](https://github.com/UtopikLab/infra-codefactory)

Chart publication and cluster deployment are separate, gated steps.

The optional chart-publish job pushes `inference` to
`oci://ghcr.io/utopiklab/inference-platform`, which produces the package
`ghcr.io/utopiklab/inference-platform/inference`. Both product repositories use
the same chart publication policy: lint, tests, and chart validation must succeed,
the event must be `push` or `workflow_dispatch` on main or a version tag, and the
repository Actions variable `HELM_CHART_PUBLISH_ENABLED` must be exactly `true`.
Tags must be exact SemVer versions with an optional leading `v`; prerelease
identifiers follow SemVer and numeric identifiers cannot have leading zeroes.
The infra Dev profile pins the
exact prerelease version `0.0.0-dev`, distinct from the mutable OCI `dev` alias.
Each eligible main publication publishes the valid SemVer `0.0.0-sha.<full-commit>`,
replaces the mutable development package `0.0.0-dev`, and moves `dev` to the
verified source digest. Tag releases never move `dev`.
Every publication retains `0.0.0-sha.<full-commit>` for exact source
resolution. The chart carries that source commit in its OCI revision
annotation. Existing release versions are never replaced; an existing source
version is reused only when its annotation matches, otherwise publication
fails. The source commit and registry digest are verified before publishing
`0.0.0-dev`, updating the `dev` alias, or reporting successful publication.
The development package has its own version and digest; `dev` aliases the
commit-addressed source package. Infra consumes the OCI chart
repository and exact version configured for the environment, using
`helm pull --version`, and fails if it is absent; it never falls back to a mutable
`dev` or `latest` alias. Product release-manifest JSON and intermediate
chart/image manifest artifacts are no longer generated. Code Factory's image
publication remains main-only; its tag runs publish charts only.
The checked-in `helm/inference/Chart.yaml` version is only a valid placeholder;
the workflow supplies the package version. Pull requests remain
validation-only. The runtime image pin is unchanged.

The workflow does not set package visibility. GitHub's read-only package
metadata does not expose UtopikLab's package-creation visibility default, so
assume a newly created package may be private. Verify the desired visibility
for the UtopikLab-owned package in GitHub before enabling the publish variable.
The infra resolution workflow also needs read access to this package; a private
package requires separately configured access there.

## Storage and Ownership in 0.2.0

Defaults preserve Namespace creation and the enabled NVIDIA dependency, but
local PV creation is now disabled. Environment overlays own node names, physical
paths, capacity, storage class/binding, routing and hardware topology.
`namespaces.create` and `inference.pv.create` independently control Namespace
and PV management. Routine deployment sets both to `false`; the NVIDIA
device-plugin remains enabled by default. Helm's
`--namespace` selects the namespace for every namespaced resource. There is no
top-level `namespace` chart value. The Namespace template also uses
`.Release.Namespace`. The locked NVIDIA dependency follows that namespace;
its namespace-local resources use that namespace and `namespaceOverride`, when
set, must match it. The dependency also renders cluster-scoped RBAC and
privileged DaemonSets, so deployment requires the corresponding cluster
permissions and NVIDIA driver/runtime support on GPU nodes.

Infra keeps `namespaces.create: false`: deployment operators must preprovision
the selected namespace separately. Routine Helm deployment does not use
`--create-namespace` or adopt namespaces; it does install the enabled GPU
plugin. Setting `namespaces.create: true` is a separate bootstrap choice, not
routine deployment permission. No namespace or context is inferred from this
chart's name.

The PVC is always rendered. Configured server profiles mount it, retaining
`LLAMA_CACHE=/models/llama.cpp/<profile-name>`. It explicitly binds
`inference.pvc.volumeName` when supplied; otherwise it binds the chart-created
PV if enabled, or allows provisioning by the cluster. `inference.pvc.name`
defaults to `inference-models`. The generic request is now `1Gi`, not `500Gi`;
set the verified capacity in the environment overlay. `storageClassName: null`
omits the field to use the cluster default; `""` explicitly disables dynamic
provisioning. Chart-created local PVs and their claims default to class `""`.
PV and PVC have `helm.sh/resource-policy: keep`;
the local PV also uses reclaim policy `Retain`. Retention leaves resources for
operator management; it is not a backup, permission grant, or automatic
adoption mechanism. A preexisting PVC must already have compatible Helm
ownership or require a separately reviewed migration; do not force adoption.

Local PV creation requires `inference.pv.nodeName` and an absolute
`inference.pv.path`. `inference.pv.name` optionally overrides the claim name.
Operator bootstrap is separate from the application release. To inspect only
the operator definitions after building dependencies, use a verified infra
values file (replace `ENVIRONMENT_VALUES` with its path):

```sh
helm template inference helm/inference --namespace inference \
	--values "$ENVIRONMENT_VALUES" \
	--set nvidia-device-plugin.enabled=false \
	--set namespaces.create=true --set inference.pv.create=true \
	--show-only templates/namespace.yaml --show-only templates/pv.yaml
```

This prints YAML only. Do not apply it over an existing PV or install a second
Helm release to take ownership. Verify the existing Namespace and retained PV
against the environment's recorded name, capacity, path, node affinity,
access mode/class, and intended claim binding.
The routine render omits Namespace/PV and includes the pinned plugin's
ClusterRole, ClusterRoleBinding, and DaemonSets, which require cluster-scoped
permissions and hostPath access. It does not render a CRD. A local PV's node
affinity places its claim consumers.
Preserve the model directory and per-GPU cache; do not resize, delete, recreate,
or adopt the live PV as part of rollout. Switching off ownership in an existing
Helm release that previously managed cluster resources requires separate
operator review of stored release manifests; new keep annotations do not
retroactively protect older revisions from deletion.

The default contains the GPU-backed `coder` and `worker` profiles (one replica
each), each with its own Deployment, fixed-size HPA, and ClusterIP Service.
UtopikLab configures the separate CPU review judge as `inference-judge-cpu`.
`inference.servers`
accepts a DNS-safe `name`, `replicas`, `modelDir`, `modelArgs`, `serverArgs`,
`resources.requests`/`resources.limits`, and `service.port`. The model and server
argument arrays are passed to `llama-server` as individual arguments. The chart
adds `--host 0.0.0.0` and `--port` from `service.port`; do not add `--host` or
`--port` in either argument array. Probes and the Service target the named HTTP
container port, so the configured port stays coherent end to end. Profiles
share the configured model PVC but retain separate native-download caches.
`inference.imagePullSecrets` remains optional.

Resource maps use Kubernetes resource names. Defaults request and limit one
`nvidia.com/gpu` per profile, matching the verified two-GPU Dev target, without
changing the existing CPU request of 1 or limit of 2. Generation and prompt
threads are explicitly 2, with one server slot and a 32768-token context pool.
The fixed HPA is 1–1 for each role. This requires an exclusive device-plugin
configuration (no time-slicing or MPS sharing). Do not set
`NVIDIA_VISIBLE_DEVICES=all` or override the plugin's assigned visibility.
`nvidia.com/gpu` requests a generic NVIDIA GPU and cannot select a physical GPU model. Selecting
different GPU models on one node requires cluster-advertised distinct resource
names or custom device-plugin/device support; profile labels alone do not pin a
physical device.

Ingress remains disabled by default. To enable it, configure `routes` entries
with `host`, `path`, and `profile` (the matching `inference.servers[].name`),
plus optional `pathType`, `className`, and annotations. Routes with the same
host are grouped into one rule and target the selected profile Service. Each
TLS host must match a route host. For a pre-provisioned certificate, the Secret
must exist in the release namespace. To request cert-manager issuance, set
`inference.ingress.certManager.enabled` and provide the existing issuer's
`issuerName`; `issuerKind` selects `Issuer` or `ClusterIssuer` (default). The
chart adds the appropriate issuer annotation but does not install cert-manager
or create the issuer. Operators must provide a working issuer, solver, ingress
controller, DNS, and any required external reachability before rollout. Do not
put issuer credentials in chart values. The chart does not assume public
exposure or authentication. Legacy `entrypoints`, `middlewares`, and `method`
values were ignored and are not supported.

The infra Dev overlay configures the two exclusive one-GPU profile workloads,
8GiB memory per pod, the existing `inference-dev` namespace, and Code Factory's
unchanged internal Service endpoints. It does not enable Ingress. These source changes do not
change chart publication/versioning or the reviewed artifact lock; publishing a
new chart and updating the infra artifact lock remain separate gated follow-up
work.

See [the GPU/runtime contract and controlled migration prerequisites](docs/inference-gpu-contract.md).
`Recreate` prevents replacement surge for each Deployment, but does **not** make
the initial migration safe while legacy pods still use unreserved GPUs.

Before rollout, publish from the chosen release tag through the existing gated
publication process, verify the resulting OCI digest/version and exact render
with the infra overlay, and commit that verified immutable pin in infra. Never substitute the old
`0.1.0` digest or deploy this source directory from infra. GPU plugin, ingress
controller/TLS, model storage, image-pull authentication, and dedicated runner
authorization must be independently verified. No publication or provisioning
has been performed by these source changes.

## CI Pipeline

Both product repositories use matching lint, Python test, Helm validation,
optional chart publication, and CI summary jobs. The test jobs share the same
step scripts, configured by a `slice` matrix dimension and repository-level
test defaults. Jobs are named `test-<slice>`. Inference installs the root
`requirements-test.txt` with no matrix overrides or service requirements;
Code Factory derives directory paths and test requirements from every slice,
including shared under `orchestrator/shared/`, without overrides. Both
workflows install one requirements file per slice. Artifact names derive
from the slice. Inference has one shared
slice collecting the entire `tests/` directory with pytest and PyYAML; Code
Factory has five isolated slices. Both matrices disable
fail-fast. Tests include unittest cases and emit a JUnit report plus a GitHub
job summary. The `python-test-results-shared` artifact is retained for 14 days, including failed
test runs that produce a report. The final summary includes publication
outcomes, even when jobs fail or are skipped.

Both workflows use the same Helm dependency preparation and lint/render scripts.
Workflow environment settings select locked dependency builds, strict lint,
values files, render settings, and expected images. Inference builds its locked
NVIDIA chart dependency before tests, validation, and packaging and uses strict
lint. Code Factory validates its example values and renders with management
enabled, checking all four service images.

Inference uses the upstream digest-pinned runtime image and therefore
has no container-build job. Code Factory additionally builds four services.
Publication remains opt-in with the existing product-specific ref rules and
coordinates. Source annotations, registry digest verification, and test-result
artifacts are retained; deployment selects charts by exact version, without
product release-manifest artifacts.

## Local Validation

```sh
helm repo add nvidia https://nvidia.github.io/k8s-device-plugin
helm dependency build helm/inference
python3 -m pip install -r requirements-test.txt
python3 -m pip install ruff
ruff check tests/
python3 -m pytest tests/ -v -ra --durations=10 --junitxml=test-results/pytest.xml
helm lint --strict helm/inference
helm template inference helm/inference --namespace inference
```