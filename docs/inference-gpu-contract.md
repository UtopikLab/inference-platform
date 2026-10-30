# Exclusive inference roles and pinned runtime contract

Source-only configuration; publication, deployment, restarts, inference,
benchmarks, model downloads, and host changes are not part of this change.

## Desired configuration

The chart defaults and UtopikLab Dev overlay have one coder and one judge/planner
replica, each requesting **and** limiting `nvidia.com/gpu: 1`. Each HPA is fixed
at 1–1. The supplied target assessment confirmed two allocatable P40 GPUs and
an NVIDIA device plugin without time-slicing. Under that exclusive allocation
policy the pods get distinct devices, not a promised physical ordinal. Neither
profile names nor Kubernetes GPU requests mean “coder gets GPU 0”. No CUDA or
NVIDIA visibility override is added; the plugin assigns the device.

Both GPU profiles select the node class with
`nvidia.com/gpu.product=Tesla-P40`. A read-only query of node metadata in
context `default` returned that exact label on `k8s-node01` on 2026-10-30.
This selects a matching node, not a physical GPU index; the device plugin may
assign either available card. The chart consumes this existing label but does
not create it or assume a particular label publisher, including GPU Feature
Discovery. Keep the label accurate on every eligible node.

CPU requests/limits remain 1/2. Generation and batch/prompt threads are both 2;
these are inference-pool sizes, not a promise that the router and all auxiliary
threads total two. Memory stays **8GiB in Dev** (generic chart default: 2GiB).
The selected model remains
`Qwen/Qwen2.5-Coder-7B-Instruct-GGUF:Q4_K_M`, supplied by existing requesters,
not eagerly loaded by the chart. No model argument, download, cache layout,
autoload policy, listener, Service, probe or model-selection change is made.

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

Thus both profiles pass:

```text
--models-max 1 --ctx-size 32768 --parallel 1 --threads 2 --threads-batch 2 --gpu-layers all
```

`--models-max 1` caps resident models in the router; it is **not** concurrency.
`--parallel 1` selects one model-server slot for the existing sequential
workload and the 32768 context pool, rather than four slots sharing that pool.
Full GPU-layer offload uses only the plugin-assigned visible GPU. This is not a
claim that every computation executes on GPU. No KV precision, batch precision,
flash-attention, fit-policy or performance-gain tuning is introduced.

This evidence establishes supported syntax and source-level forwarding, not
live child arguments or measured single-P40 peak memory. Those remain rollout
verification gates. Do not silently reduce context, change quantization, or
fall back to shared GPUs if memory validation fails.

## Rollout and rollback prerequisites — separately authorized maintenance only

1. Review/publish the changed chart through the normal gated process, then
   validate the **actual published artifact** with the exact Dev overlay and
   schema. The currently configured `0.0.0-dev` reference is not proof of
   source/package equivalence or publisher provenance.
2. Verify context `default`, release `inference`, namespace `inference-dev`,
   runner permissions, stored release manifests, namespace/release ownership,
   PV/PVC binding and retention, Secret references (never values), pinned image,
   two allocatable exclusive GPUs and absence of competing/unreserved users.
   Do not reinstall/adopt the existing device plugin or retained storage.
3. Reserve a maintenance window and quiesce callers for **both** roles.
   Existing old pods have no GPU reservations and can see/use both devices.
   New resource requests cannot protect against them. A reviewed procedure
   must suspend autoscaler/other reconciliation that would undo a stop, then
   stop **all old coder and judge pods and their model processes before any
   new pod starts**. Merely upgrading with `Recreate`, scaling one role at a
   time, or scaling to zero while a 1–1 HPA remains active is insufficient.
   No such live transition is authorized here.
4. Only after the all-stopped barrier, start the two new roles under exclusive
   GPU allocation. `Recreate` is retained to avoid a third pod waiting forever
   for a surge GPU on subsequent upgrades. It serializes replacement within
   each Deployment, not across the whole release; downtime is expected.
5. Use bounded readiness waits (existing deployment tooling: 5m). Report coder
   and judge separately. Verify assigned device UUIDs are distinct, no old
   processes remain, actual model-child flags/threads/context/slots/offload,
   health, endpoints, preserved caches, and the unchanged selected model.
   A separately authorized representative workload must establish peak
   single-P40 VRAM and 8GiB container-memory fit. Router `/health` alone does
   not prove lazy-loaded model readiness.
6. Preapprove rollback artifact/overlay, maintenance permissions, recovery
   bounds and retained-model storage checks. Stop and inspect on failure.
   No automatic rollback or storage mutation is authorized. A rollback to old
   manifests without GPU requests/visibility isolation also needs an
   all-stopped barrier; a blind `helm rollback` can reintroduce co-residence.

No live readiness, throughput, VRAM peak, or latency improvement is claimed.
