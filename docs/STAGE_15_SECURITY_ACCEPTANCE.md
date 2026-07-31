# Stage 15 security acceptance

## Status

Stage 15 is accepted for the local MVP when
`tests/run_stage15_acceptance.py` finishes successfully. The gate is fully
offline and does not verify the availability of VEP, MyVariant.info, NCBI,
UCSC GenCC, HPO, or the configured LLM provider.

## Implemented controls

- Repository and Git-patch scanning for credentials, private keys, environment
  files, databases, logs, reports, and uploaded clinical data.
- Bounded VCF and gzip upload validation before storage.
- Randomized, confined temporary storage with cleanup on success and failure.
- Symlink rejection and restrictive file-mode requests for private storage.
- Clinical-data minimization before public results, persistence, reports, and
  LLM calls.
- Rejection of raw VCF, genotype, sample, patient, and provider-payload fields
  at public and LLM boundaries.
- Credential-free HTTPS endpoint validation and explicit TLS certificate
  verification for every outbound request.
- Loopback-only Streamlit binding, CORS and XSRF protection, hidden internal
  errors, disabled static serving, bounded messages, and disabled telemetry.
- Compilation, dependency consistency, focused security tests, complete
  regression tests, and an 80% minimum coverage gate.

## Acceptance command

```powershell
.\.venv\Scripts\python.exe tests\run_stage15_acceptance.py
```

The final line must be:

```text
Stage 15 offline security acceptance: PASSED
```

## Production blockers

This acceptance is not a certification and does not authorize real patient
data in production. Before institutional deployment:

1. Add authentication, authorization, session-expiration, and user audit
   trails.
2. Terminate HTTPS at an authenticated reverse proxy on the same host; keep
   Streamlit bound to `127.0.0.1`.
3. Apply institution-approved NTFS ACLs to uploads, reports, logs, databases,
   backups, and HPO caches.
4. Define retention, deletion, backup, recovery, and incident-response
   policies.
5. Complete a legal and privacy review of each external service and LLM
   provider before transmitting clinical evidence.
6. Run an approved dependency-vulnerability scanner with current advisory
   data in the deployment environment.
7. Perform an independent penetration test and clinical safety review.

## Live validation

Live provider checks remain separate because they require network access and
may transmit the configured test evidence. Use only synthetic or public test
variants and follow the manual Stage 13 validation commands in `README.md`.
