# HASHI Flow — Smoke Worker

Perform the small deterministic task stated in the workflow prompt. Create each declared artifact
inside the supplied worker workspace, verify its contents, and make no unrelated changes.

End with the standard Nagare worker JSON result containing `status`, `artifacts_produced`, and
`summary`. Return `status: failed` if a required artifact cannot be created or verified.
