# inference-platform

This repository packages the `inference` Helm chart for the upstream llama.cpp
server runtime. It does not maintain a forked inference image or choose an
organization's roles, cluster, namespace, storage, or hardware topology.
Consuming deployment configuration supplies those values; infrastructure
automation owns environment selection and release operations.

Chart publication and cluster deployment are separate, gated steps.

The optional chart-publish job publishes `inference` to its configured OCI
registry. A consuming infrastructure configuration selects the repository and
exact version. Lint, tests, and chart validation must succeed,
the event must be `push` or `workflow_dispatch` on main or a version tag, and the
repository Actions variable `HELM_CHART_PUBLISH_ENABLED` must be exactly `true`.
Tags must be exact SemVer versions with an optional leading `v`; prerelease
identifiers follow SemVer and numeric identifiers cannot have leading zeroes.
The development version `0.0.0-dev` is distinct from the mutable OCI `dev` alias.
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
commit-addressed source package. Consumers use `helm pull --version` for the
configured exact version and must not fall back to a mutable
`dev` or `latest` alias. Product release-manifest JSON and intermediate
chart/image manifest artifacts are not generated.
The checked-in `helm/inference/Chart.yaml` version is only a valid placeholder;
the workflow supplies the package version. Pull requests remain
validation-only. The runtime image pin is unchanged.

The workflow does not set package visibility. Verify the desired visibility
and ensure consuming infrastructure has read access before enabling publication.

## Storage and Ownership in 0.2.0

The chart creates a Namespace by default, but local PV creation is disabled.
Consuming values own node names, physical paths, capacity, storage
class/binding, routing, server profiles, and hardware topology.
`namespaces.create` and `inference.pv.create` independently control Namespace
and PV management. Helm's
`--namespace` selects the namespace for every namespaced resource. There is no
top-level `namespace` chart value. The Namespace template also uses
`.Release.Namespace`.

The chart does not infer a namespace or context from its name. Cluster access
and namespace provisioning remain the consuming deployment's responsibility.
The NVIDIA driver and device plugin are cluster prerequisites for profiles that
request NVIDIA GPUs. They are intentionally not installed or owned by this
workload chart and should be managed through the cluster's infrastructure
process.

The PVC is always rendered. Configured server profiles mount it, retaining
`LLAMA_CACHE=/models/llama.cpp/<profile-name>`. It explicitly binds
`inference.pvc.volumeName` when supplied; otherwise it binds the chart-created
PV if enabled, or allows provisioning by the cluster. `inference.pvc.name`
defaults to `inference-models`. The generic request is `1Gi`; set the verified
capacity in the environment overlay. `storageClassName: null`
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
the namespace and PV definitions, use the reviewed deployment values file
(replace `ENVIRONMENT_VALUES` with its path):

```sh
helm template inference helm/inference --namespace inference \
	--values "$ENVIRONMENT_VALUES" \
	--set namespaces.create=true --set inference.pv.create=true \
	--show-only templates/namespace.yaml --show-only templates/pv.yaml
```

This prints YAML only. Do not apply it over an existing PV or install a second
Helm release to take ownership. Verify the existing Namespace and retained PV
against the environment's recorded name, capacity, path, node affinity,
access mode/class, and intended claim binding.
The application chart does not render cluster-scoped resources or host paths.
A local PV's node affinity places its claim consumers.
Preserve the model directory and per-GPU cache; do not resize, delete, recreate,
or adopt the live PV as part of rollout. Switching off ownership in an existing
Helm release that previously managed cluster resources requires separate
operator review of stored release manifests; new keep annotations do not
retroactively protect older revisions from deletion.

Each configured `inference.servers` profile has its own Deployment, fixed-size
HPA, and ClusterIP Service. The chart provides no default server roles.
Services use client-IP affinity so a caller's native model preparation and
inference requests reach the same router process, whose model registry is
process-local.
Profiles accept a DNS-safe `name`, `replicas`, `modelDir`, `modelArgs`, `serverArgs`,
`nodeSelector`, `resources.requests`/`resources.limits`, and `service.port`.
`nodeSelector` is an optional per-profile map of node-label keys to string
values; it selects a hardware class, not an individual GPU. The model and server
argument arrays are passed to `llama-server` as individual arguments. The chart
adds `--host 0.0.0.0` and `--port` from `service.port`; do not add `--host` or
`--port` in either argument array. Probes and the Service target the named HTTP
container port, so the configured port stays coherent end to end. Profiles
share the configured model PVC but retain separate native-download caches.
`inference.imagePullSecrets` remains optional.

Resource maps use Kubernetes resource names and are supplied per profile.
When a profile requests `nvidia.com/gpu`, the cluster must advertise that
resource and provide exclusive device allocation appropriate to the workload.
The device plugin assigns visible devices; do not set
`NVIDIA_VISIBLE_DEVICES=all` or override the plugin's assigned visibility.
`nvidia.com/gpu` requests a generic NVIDIA GPU and cannot select a physical GPU model. Selecting
different GPU models requires cluster-advertised distinct resource names or
device support; profile labels alone do not pin a physical device.

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

See [the GPU/runtime contract and migration prerequisites](docs/inference-gpu-contract.md).
`Recreate` avoids replacement surge for one profile, but rollout sequencing must
account for available GPU capacity and any existing unreserved workloads.

## CI Pipeline

CI runs Python lint, tests, strict Helm lint/render validation, and optional
chart publication. The runtime image is upstream and digest-pinned, so this
repository does not build a container image.

## Local Validation

```sh
python3 -m pip install -r requirements-test.txt
python3 -m pip install ruff
ruff check tests/
python3 -m pytest tests/ -v -ra --durations=10 --junitxml=test-results/pytest.xml
helm lint --strict helm/inference
helm template inference helm/inference --namespace inference
```