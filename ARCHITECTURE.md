# HASHI Architecture

The canonical architecture and engineering guideline is
[`docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md`](docs/HASHI_LAYERED_RUNTIME_BOUNDARIES.md).
The normative Python/ABI and transactional function-generation decision is
[`docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md`](docs/HASHI_PYTHON_RUNTIME_COMPATIBILITY.md).

In short:

```text
stable process core
    -> isolated, verified function generations
        -> local platform adoption
            -> local instance configuration
```

Changes should be local, derived from a single fact owner, and reloadable with
`/reboot` unless they alter process bootstrap itself. Contributor workflow and
required checks are in [`CONTRIBUTING.md`](CONTRIBUTING.md).
