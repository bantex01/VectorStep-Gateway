# Kubernetes manifests — VectorStep-Gateway

Plain, copy-and-adapt YAML — the same spirit as `samples/`, not a generic
chart. Assumes you already have images published to GHCR (see
`../../../VectorStep/RELEASING.md` for how a tag becomes an image) — apply in
this order:

```sh
kubectl create secret generic vectorstep-gateway-secrets \
  --from-literal=ANTHROPIC_API_KEY=...
kubectl apply -f pvc.yaml
kubectl apply -f configmap.example.yaml   # copy + edit first — see the file's header comment
kubectl apply -f deployment.yaml
kubectl apply -f service.yaml
```

Deploy VectorStep (`../../../VectorStep/deploy/k8s/`) alongside it — its
config points at this Gateway's Service (`vectorstep-gateway:18780`).

**Why no Helm chart.** These manifests are the ground truth a chart would
template. Templating them before there's a second real user to justify the
abstraction is premature — this is a deliberate deferral, not an oversight.
Revisit once there's a concrete reason (a second environment, a second
operator) that a raw `kubectl apply` doesn't serve well.

**Image tags** are what a GitOps controller (Argo CD, Flux, a `kubectl set
image` job) would watch to pull new releases — see
`../../../VectorStep/RELEASING.md` for what a tag means and the
compatibility rule between VectorStep and the Gateway (they deploy as a
matched pair — there is no wire-version negotiation between them yet).
Continuous deployment itself is out of scope here; these manifests are its
input.
