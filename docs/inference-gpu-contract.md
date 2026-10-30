# GPU and runtime contract

The chart renders only the llama.cpp profiles supplied by the consuming
environment. It does not install or configure NVIDIA drivers, the NVIDIA
device plugin, node labels, or GPU sharing policy. Those are cluster
prerequisites managed independently from an inference release.

Profiles that request `nvidia.com/gpu` require that resource to be advertised
by the cluster and an allocation policy suitable for the workload. A
`nodeSelector` selects labeled nodes; neither profile names nor resource
requests select a physical GPU ordinal. The device plugin controls container
device visibility. Do not override that assignment with
`NVIDIA_VISIBLE_DEVICES=all`.

CPU/memory resources, replicas, arguments, storage, and node selection are
per-profile values. `service.port` configures the llama-server listener, probes,
Service, and optional Ingress backend together. Model selection and downloads
remain runtime/API concerns rather than organization defaults in the chart.

## Runtime support evidence (read-only source inspection, 2026-10-29)

The unchanged image is
`ghcr.io/ggml-org/llama.cpp:server-cuda@sha256:1bacc872b168d2d10bfd8c220336f5a2cd464c07605936ec146ee5198832ffce`.
The supplied runtime assessment identifies build 11783, commit `339f3242c`,
CUDA 12.8.90. The following **exact commit**, not upstream main, was inspected:

- [`common/arg.cpp`](https://github.com/ggml-org/llama.cpp/blob/339f3242c/common/arg.cpp):
  lines 1520–1538 define `--threads` (generation) and `--threads-batch`
  (batch/prompt); 1642–1651 define `--ctx-size`; 2548–2556 define `--parallel`
  as server slots; 2803–2819 accept `--gpu-layers all` and map it to full
  layer offload (`n_gpu_layers = -2`); 3697–3710 define the router model cap
  and default-preserving autoload switch.
- [`tools/server/server-models.cpp`](https://github.com/ggml-org/llama.cpp/blob/339f3242c/tools/server/server-models.cpp):
  `server_models` constructs `base_preset` from the router's CLI;
  `unset_reserved_args` removes router-only options including `--models-max`,
  **not** threads, batch threads, context, parallel or GPU layers.
  `load_models` merges the router base preset over every model preset.
  `server_model_meta::update_args` sets child host/port/alias and calls
  `preset.to_args(bin_path)` to construct the model subprocess arguments.
- [`common/preset.cpp`](https://github.com/ggml-org/llama.cpp/blob/339f3242c/common/preset.cpp):
  `load_from_args` uses `common_params_to_map`; `to_args` emits each option's
  long name and value. `common_params_to_map` in `common/arg.cpp:1190–1251`
  maps CLI options even without an environment alias, including batch threads.
  In particular, the offload option is serialized to its equivalent canonical
  child spelling `--n-gpu-layers all`.
- [`tools/server/README.md:1972–1978`](https://github.com/ggml-org/llama.cpp/blob/339f3242c/tools/server/README.md#using-multiple-models)
  documents passing default model-instance arguments via the router CLI.

For a profile configured with these arguments:

```text
--models-max 1 --ctx-size 32768 --parallel 1 --threads 2 --threads-batch 2 --gpu-layers all
```

`--models-max 1` caps resident models in the router; it is **not** concurrency.
`--parallel 1` selects one model-server slot and its configured context pool.
Full GPU-layer offload uses only the plugin-assigned visible GPU. This is not a
claim that every computation executes on GPU. No KV precision, batch precision,
flash-attention, fit-policy or performance-gain tuning is introduced.

This evidence establishes supported syntax and source-level forwarding, not
live child arguments or measured peak GPU memory. Those remain rollout
verification gates. Do not silently reduce context, change quantization, or
fall back to shared GPUs if memory validation fails.

## Rollout and rollback prerequisites — separately authorized maintenance only

1. Review/publish the changed chart through the normal gated process, then
   validate the **actual published artifact** with the intended values and
   schema. A mutable development reference is not proof of source/package
   equivalence or publisher provenance.
2. Verify the selected cluster context, release and namespace, runner
   permissions, existing ownership, stored release manifests, PV/PVC binding
   and retention, Secret references (never values), and the pinned image.
   Confirm NVIDIA drivers, device plugin, advertised GPU resources, allocation
   policy, and available capacity independently for GPU-backed profiles.
3. If replacing workloads that used GPUs without resource reservations, plan a
   controlled stop of all affected pods and model processes before starting
   replacements. New resource requests cannot protect against old unreserved
   workloads. Suspend any reconciliation that would undo the stop; `Recreate`
   alone does not provide a release-wide all-stopped barrier.
4. Inspect stored manifests before removing resources formerly managed by the
   inference release. In particular, changing plugin ownership can cause Helm
   to remove cluster-scoped resources; do not adopt, remove, or reinstall a
   separately managed plugin without an explicitly reviewed migration.
5. Use bounded readiness waits. Verify actual model-child flags, threads,
   context, slots and offload; health, endpoints, preserved caches, assigned
   devices, and representative memory fit. Router `/health` alone does not
   prove lazy-loaded model readiness.
6. Preapprove rollback artifact/values, permissions, recovery bounds and
   retained-storage checks. Stop and inspect on failure. No automatic rollback
   or storage mutation is implied. A rollback that restores unreserved GPU
   workloads also requires a controlled stop to prevent GPU co-residence.

No live readiness, throughput, VRAM peak, or latency improvement is claimed by
source renders or chart tests.
