# Observation contract

`vision_inspect` returns JSON containing:

- `answer`: the provider's direct answer to the visual question.
- `observations`: concrete visible evidence supporting the answer.
- `uncertainties`: ambiguities, low-confidence identifications, or missing detail.
- `model`, `detail`, `normalized_size`, and `elapsed_ms`: diagnostic metadata.

Use `answer` as a concise description, support important claims with `observations`, and carry `uncertainties` into the final response. A successful tool call is evidence, not proof of identity, intent, causality, diagnosis, legality, or other hidden facts.

An `Error:` result means no reliable visual observation was produced. Explain the limitation or retry once when the error is plausibly transient. Do not substitute a guess.
