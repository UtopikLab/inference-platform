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