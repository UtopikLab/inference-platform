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
Pull requests remain validation-only. Chart versions follow SemVer in
`helm/inference/Chart.yaml`; the current version remains `0.1.0`.

The workflow does not set package visibility. GitHub's read-only package
metadata does not expose UtopikLab's package-creation visibility default, so
assume a newly created package may be private. Verify the desired visibility
for the UtopikLab-owned package in GitHub before enabling the publish variable.