# Vision skill boundary

Functional owner: PAO. Engineering layer: Functions, implemented as a standalone skill CLI. Provider identity, secrets and domain-specific prompts/regions belong in private instance configuration. Core and shared model/port registries are unchanged.

The client requires explicit endpoint/model selection, rejects redirects and bounds input/response data. Framing is a configurable evidence-matching heuristic with crop corroboration and explicit uncertainty, not a general detector or proof of image content. Public assets contain only reusable code, configuration schema and synthetic test examples.

Adoption and live provider acceptance are separate from source review. The offline suite contacts only temporary test servers and does not load models, use personal images or change services.
