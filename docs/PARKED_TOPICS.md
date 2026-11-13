# Parked conversation recovery

Owner: PCM. Engineering layer: Functions. No Core change.

`/park chat [title]` preserves the current Session's recent context, last exchange and a resumable topic. The caller must be authorized and the Agent must be idle. A busy Agent does not start summary generation or create a topic.

Summary generation is optional enrichment with an eight-second deadline. A slow or failed helper falls back to the already captured recent context and last messages, honoring an explicit title. It does not fail the save or wait indefinitely past the frontend command deadline. One command creates one topic and sends its saved receipt only after durable storage succeeds. Cancelling the command remains cancellation and does not create a topic. Storage failures remain failures; fallback does not turn an unsuccessful write into success.

Successful generated summaries retain their existing fields. Topic follow-ups, guarded deletion and `/load` retain their existing owners and persistence rules. This repair neither retries an old command with an uncertain outcome nor reconstructs an original title from a later successful operation.

Focused validation: `tests/test_park_summary_timeout.py` covers slow and failed enrichment, one persistent save, fast summaries, cancellation and busy Agents. Live acceptance uses a new owned Session action and reads back the one saved topic before cleanup. Source checks, running Function adoption and frontend observations are recorded separately.
