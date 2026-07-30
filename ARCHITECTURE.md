# HASHI Architecture

The canonical architecture and engineering guideline is
[`docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).

In short:

```text
stable process core
    -> hot-reloadable functions
        -> local platform adoption
            -> local instance configuration
```

Changes should be local, derived from a single fact owner, and reloadable with
`/reboot` unless they alter process bootstrap itself. Contributor workflow and
required checks are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
