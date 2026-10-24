# inference-platform

Inference deployment source for the Code Factory system. This repository owns
the `inference` Helm chart and its integration with the upstream llama.cpp
runtime; it does not maintain a forked inference image.

- Helm chart: `helm/inference/`
- Application components: the
	[code-factory repository](https://github.com/UtopikPrompt/code-factory)
- Environment selection and deployment operations: the
	[infra-codefactory repository](https://github.com/UtopikLab/infra-codefactory)

Chart publication and cluster deployment are separate, gated steps.

The optional chart-publish job pushes `inference` to
`oci://ghcr.io/utopiklab/inference-platform`, which produces the package
`ghcr.io/utopiklab/inference-platform/inference`. It runs after chart validation
on pushes to `main` or a `workflow_dispatch` run on `main`, and only when the
repository Actions variable `HELM_CHART_PUBLISH_ENABLED` is exactly `true`.
Pushes of `vX.Y.Z` or `X.Y.Z` release tags publish using the tag's SemVer as
the chart version (the optional leading `v` is removed). Main-branch pushes
and manual runs publish with a unique `0.0.0-main.<run>.<attempt>` version.
Each publication also creates `0.0.0-sha.<full-commit>` for exact source
resolution. The chart carries that source commit in its OCI revision
annotation. Existing release versions are never replaced; an existing source
version is reused only when its annotation matches, otherwise publication
fails. Infra resolves the chart by the exact version derived from the selected
Git ref and fails if it is absent; it never falls back to a mutable `main` or
`latest` alias.
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
and PV management. Routine deployment must set both to `false` and set
`nvidia-device-plugin.enabled=false` in its environment overlay. Use release
namespace `inference` with the default `namespace: inference` values; changing
the release namespace alone does not relocate every resource.

The PVC is always rendered. Configured server deployments mount it, retaining
`LLAMA_CACHE=/models/llama.cpp/<deployment-name>`. It explicitly binds
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
The routine render omits Namespace/PV and contains no cluster RBAC, CRD,
DaemonSet, or hostPath. A local PV's node affinity places its claim consumers.
Preserve the model directory and per-GPU cache; do not resize, delete, recreate,
or adopt the live PV as part of rollout. Switching off ownership in an existing
Helm release that previously managed cluster resources requires separate
operator review of stored release manifests; new keep annotations do not
retroactively protect older revisions from deletion.

The default now contains one server profile rather than assuming two GPUs, and
no image-pull Secret reference. Set `inference.configMaps` and
`inference.imagePullSecrets` in infra. Ingress is now disabled by default;
enable `inference.ingress.enabled` with an explicit `host`, optional `className`,
`annotations`, and `tls`. The previously hardcoded Traefik annotations and host
are no longer supplied by the chart. Legacy `entrypoints`, `middlewares`, and
`method` values must move into `annotations`; these were previously ignored.
The k8s-node01 overlay in infra preserves the prior two-server/cache layout,
static storage binding, pull-secret reference and routing settings.

Before rollout, publish from the chosen release tag through the existing gated
publication process, verify the resulting OCI digest/version and exact render
with the infra overlay, and commit that verified immutable pin in infra. Never substitute the old
`0.1.0` digest or deploy this source directory from infra. GPU plugin, ingress
controller/TLS, model storage, image-pull authentication, and dedicated runner
authorization must be independently verified. No publication or provisioning
has been performed by these source changes.

## Local Validation

```sh
helm repo add nvidia https://nvidia.github.io/k8s-device-plugin
helm dependency build helm/inference
python3 -m pip install "PyYAML>=6,<7" ruff
ruff check tests/
python3 -m unittest discover -s tests -v
helm lint --strict helm/inference
helm template inference helm/inference --namespace inference
```