# Historical Phone translation discovery snapshot

Status: historical. This records the pre-41a91616 catalogue audit, not the current capability contract.

At baseline `ea37129b`, the checkout had Phone context handoff and Compute speech selection but no separate Phone translation provider contract. That absence was explicitly reported during the FYI migration instead of inventing an endpoint.

Main subsequently added HY-SimulMT through PR #132. The current authority is [Phone simultaneous interpretation](HASHI_PHONE_INTERPRETATION.md), with [Compute directory compatibility](HASHI_COMPUTE_SERVICE_DIRECTORY.md#compatibility-and-truthfulness). Its experimental source qualification does not prove adoption by a running instance.

Interpretation, conversational Phone context/delegation, voice selection and transcription language have distinct responsibilities. Consult the current contract and actual deployed generation; this historical audit cannot override them.
