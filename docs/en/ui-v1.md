# pgbk web UI v1

Draft specification, 6 November 2026. Integration baseline is pgbk 0.3.3. This English specification governs development and acceptance of the standalone UI. The [Russian translation](../ru/ui-v1.md) is provided for reference.

The administrator selects a profile, checks the next scheduled backup, starts a one-off backup and follows its Job to the published artifacts and their recovery instructions. Navigation has three sections — **Backups**, **Schedules** and **Jobs**. Each screen identifies the profile and source. Starting a backup is the only backup-management write operation in v1.

## Integration with pgbk 0.3.3

For logical profiles with `logical.globals: enabled`, the worker exports globals before database archives and explicitly passes `targets.<id>.maintenanceDatabase` to pg_dumpall. The profile launch confirmation shows the configured globals policy and maintenance database from the referenced configuration, including the default `postgres` when omitted. Preserve the literal database name when displaying it. Configuration changes follow the existing deployment path. See [globals export prerequisites and diagnostics](operations.md#globals-export-failures).

A failed globals export prevents publication of the copy. Recognized permission/authentication failures in both direct and staged execution produce `ACCESS_DENIED`. Show the recorded reason and a link to the prerequisite guidance. The administrator should check the configured maintenance database and the backup role’s access. For historical generic `TOOL_FAILED` records, display the recorded code and leave the underlying cause unknown.

In 0.3.3 the terminal JSON error on worker stderr includes `tool` and `toolExitCode` when available, while persisted terminal S3 events contain `reasonCode` and `failedPhase`. The v1 Job view consumes the persisted fields. Optional tool details require a future structured diagnostic adapter. Distinguish an upstream worker `ACCESS_DENIED` from a rejected UI session or a forbidden UI API operation.

Artifact schema version 1, publication markers and per-backup recovery notes are the integration contracts. Accept older copies that pass these format checks and use their stored instructions. The [release evidence](compatibility.md) describes the validation available for 0.3.3.

## Deployment and access

- Ship a separate image and Helm chart, `pgbk-ui`, with its own Deployment, Service, ServiceAccount and upgrade lifecycle. One deployment serves one pgbk installation in one cluster. Scheduled Jobs continue during UI outages or removal. Uninstall only UI-owned resources.
- A single replica with SQLite on a dedicated PVC suffices for the manual request journal and catalog index in v1. Preserve the journal across Pod restarts. The index is rebuildable from S3. Upgrade with one active replica.
- The browser connects over HTTPS to a same-origin web server and API. Only the server accesses Kubernetes and S3. The administrator configures the installation ID, Helm release, namespace, allowed profiles, corresponding sources and allowed S3 prefixes. Cross-check the profile and source ID against the pgbk configuration referenced by the current backup CronJob. On mismatch, disable launch for that profile and show the discrepancy in Schedules.
- v1 has one application identity, **Administrator**, authorized to read the configured installation and start backups for its allowlisted profiles. Require a valid administrator session on every data and launch endpoint, including deep links and retries. Recheck installation, profile, namespace and permitted action server-side.
- Store the administrator username and password together in one installation-specific Vault KV secret. Helm values contain only the Vault address, auth role and secret path. The backend authenticates to Vault using its Kubernetes ServiceAccount and a Vault policy limited to reading this secret. The administrator submits credentials over HTTPS. The server compares them to the current Vault values, rate-limits login attempts and returns a generic error for invalid credentials. Keep credential values out of Git, Helm releases, SQLite, browser storage and logs.
- On login, issue an opaque, expiring server-side session with a Secure, HttpOnly, SameSite cookie. Bind it to the Vault secret version. Check the current version before authorizing protected requests. Rotation invalidates previous sessions and credentials. Reject access if the version cannot be checked, and report authentication temporarily unavailable. Logout revokes the session. Protect browser writes against CSRF. Audit the authenticated administrator username and request ID.
- Provide separate read-only S3 credentials limited to listing the allowed prefixes and reading catalog metadata and the recovery notes declared for each run. Backup S3 credentials and PostgreSQL credentials belong to the worker. Grant Kubernetes read access to the necessary CronJobs, Jobs and named ConfigMaps. Enable Job creation only in the backup namespace when manual launch is enabled. Namespace RBAC allows arbitrary Job creation there, so require an admission check for the UI identity that matches the Job spec to a trusted backup template, including image, command/args, SA, env and volumes. Deploy in view-only mode without that restriction. The UI requires no Secret, CronJob write, Lease or Pod log access.

## Backups and artifact details

Read the catalog at `<prefix>/v1/<installationId>/<sourceId>/<profileId>/`. A copy's identity includes that entire scope and its `runId`. List completion time with zone, profile, source, kind, manual/scheduled trigger, size and restore verification result. Sort by descending `completedAt` and `runId`, breaking ties by scope. Filter by profile, source, kind and time. Each row opens a permanent detail link.

Publish a row after validating `runs/<runId>/complete.json` against `manifest.json`, including schema version, scope, run ID, completion time and the SHA-256 of the manifest bytes. Apply pgbk's catalog format checks. Check for `deletion.json`, including when reopening a detail page. Report malformed or mismatched records separately with their run ID and reason code. An event-only run may appear in diagnostics. A link opened after retention removes the copy shows “Copy removed or unavailable”.

| Detail section | Contents |
|---|---|
| Summary | Run ID, start and completion times, duration, kind, PostgreSQL major, worker image, file count and total size. Distinguish snapshot time and its estimation basis from completion time. Show per-database values for logical copies. |
| Artifacts | File ID, filename, purpose, format, size in bytes and full SHA-256 recorded in the manifest. Open a row to inspect its JSON entry in `artifacts[]`. For logical files, show database, full or filtered scope, compression, transfer settings, recorded selection, archive inventory and known prerequisites when present. |
| Metadata | Separate views of the stored `manifest.json`, `complete.json` and verification records from `verification/<runId>/`. Render structured JSON with key search, within profile scope. Project allowed fields on the server, identify redactions and escape text. Mask internal addresses, Secret references and selection/assertion SQL even in nested objects. Label missing fields in older manifests “Unavailable”. |
| Restore verification | Apply pgbk's state calculation to the captured policy and attempt records. Show due time, active and most recent completed attempt, outcome and reason. Display publication, Job execution and restore verification independently. |

List and detail reads use size-limited JSON and metadata checks equivalent to `Catalog.describe(verify=False, check_objects=False)`. The full 0.3.3 `Catalog.list()/view()` reads archive bodies and requires a separate metadata-only read path for the UI. “Manifest verified” describes the check against the publication marker. Label artifact hashes “SHA-256 from manifest”. Page the list API and refresh a metadata index in the background across allowed prefixes with bounded concurrency. Show refresh time, isolate prefix failures and mark partial results during the initial scan.

## Recovery order tab

The source for this tab is the recovery note published with the selected backup. In 0.3.3 the worker writes `runs/<runId>/recovery/BACKUP-RESTORE.md` (English) and `BACKUP-RESTORE.ru.md` in the same directory (Russian), verifies their bytes in S3 and records each filename, size and SHA-256 in `manifest.json` under `recoveryInstructions[]` before publishing `complete.json`. The notes describe this specific logical or physical backup. The physical note may delegate extraction and verification to the recovery helper.

On opening the tab, revalidate publication and scope, look up the selected language in the verified manifest and retrieve that single note from the run’s recovery directory. Verify size (maximum 8 MiB) and SHA-256 before rendering it as safe text. Default to English and offer the stored Russian translation. Preserve the note’s section order, filenames, commands and prerequisites. The administrator can open the artifact details alongside it. Keep ordering decisions with the backup writer.

For a legacy manifest without `recoveryInstructions`, show “Recovery instructions were not stored for this backup”. A declared note that is missing or fails validation produces an integrity error for that note. An S3 read failure is shown as temporarily unavailable. In each case leave the instructions unavailable and omit generated substitutes. Other validated metadata remains visible.

## Schedules and one-off launch

Show one row per allowed profile. Read the actual `<release>-<profile>-backup` CronJob in the installation namespace. Columns cover profile, source, kind, `spec.schedule`, `spec.timeZone`, `spec.suspend`, last scheduled time from `status.lastScheduleTime` and calculated next occurrence. Compute forecasts in the cron time zone, including clock changes. Show a suspended schedule as “Paused” with no next occurrence. Distinguish a missing template and access errors from suspension. Exclude retention, reconcile and utility templates from the backup schedule.

“Run once” on a profile opens confirmation with profile, source and kind. A valid suspended backup template supports manual launch. On acceptance, open the resulting Job with name, namespace and UID. Preserve the next scheduled occurrence.

Recheck authorization and the current backup template on the server. Clone its jobTemplate, including the pinned ConfigMap reference, and set `pgbk.io/trigger=manual` on Job and Pod template. Accept only the selected profile ID and an idempotency key. Reject extra fields including host, Secret, Job YAML, cron and worker arguments. The worker acquires its existing source and repository Leases. Surface worker outcome `SkippedBusy` and its reason code on contention. Successful creation acknowledges Kubernetes acceptance.

Extend `Kubernetes.launch` with stable request identity beyond its existing `generateName`. Before Job creation, atomically reserve the key, subject, profile and selected template version in a durable request journal. Use a deterministic Job name and request label. On double click, concurrent submission or lost response, return the original Job after verifying identity. Reusing the key for another profile is a conflict. Return the recorded result after Job TTL expiry. If the outcome is ambiguous and the Job has disappeared, show “Outcome unknown” and offer event inspection before an intentional new request. Support a 24-hour retry window and reject older keys by server issuance time. Disable launch when the journal is unavailable. Audit subject, time, profile, decision, request ID and Job name/UID or error code.

## Jobs

List Jobs for the allowed installation and profiles in explicitly authorized namespaces, including configured verify/restore namespaces when authorized. Default to the backup operation. “All operations” includes utility Jobs. Columns cover namespace/name, profile, operation, trigger (scheduled/manual/unknown), creation/start time, duration and status. Filter by profile, operation and status. Refresh active details and display the last successful read time.

- Derive Kubernetes status from Job conditions and counters. Terminal Complete/Failed conditions take priority, followed by suspension, active execution and pending start. Surface read errors. Read operation from `spec.template.metadata.labels["pgbk.io/operation"]` and scope from installation/profile labels. Determine trigger from the manual annotation and CronJob owner reference. Label ambiguous cases “Unknown”.
- Separately show the latest recorded worker phase, timestamp and safe reason code and failed phase from catalog events. Before the first event show Kubernetes status alone. Link to the copy through namespace and Job UID in catalog events containing the run ID in the same scope. Leave uncorrelated records unlinked. Enable “Open backup” only after checking the marker. Include a reverse link from the copy to the Job or its historical record.
- TTL and history limits bound the live Job list. S3 events and the manual request journal can supply historical entries labelled “Job removed”. Distinguish confirmed removal from API access failure. Consider full log streaming, Job cancellation, archive downloads, restore/verify launches and schedule/profile editing after v1. Future logs should consume the main specification's REQ-047 contract.

## Minimum HTTP API

All paths have prefix `/api/v1` and one installation scope. List responses contain `items`, an opaque `nextCursor`, refresh time and partial errors. Bind cursors to filters and authorization scope. Errors carry a safe reason code and request ID.

| Request | Result |
|---|---|
| POST /session, DELETE /session | Authenticate with the Vault-backed administrator credentials, or end the current session. |
| GET /profiles, GET /schedules | Authorized profiles, launch availability and effective schedules. |
| GET /backups | Page of published copy summaries and separate diagnostics. |
| GET /profiles/{profileId}/sources/{sourceId}/backups/{runId} | Metadata, artifacts, available recovery note languages and verification outcome. Resolve S3 keys on the server within the known scope. |
| GET /profiles/{profileId}/sources/{sourceId}/backups/{runId}/recovery/{language} | Verified stored note for en or ru. Return a reason distinguishing a legacy omission, an integrity error and a temporary read failure. |
| POST /profiles/{profileId}/backups | Idempotency-Key header and empty body. Return 201 for creation, 200 for confirmed replay and 202 for pending reconciliation. |
| GET /jobs, GET /jobs/{namespace}/{uid} | Job page or single Job details including its correlated copy when available. |

Issue a key once when opening the confirmation form via GET /profiles/{profileId}/launch-token. Disable response caching. Write errors include 400 for extra fields, 403 for denied launch, 409 for key conflicts or incompatible templates and 503 for dependency outages. Return 401 for missing or invalid sessions and 503 for an unavailable authentication dependency. Return 404 for authorized direct reads outside the permitted scope.

## Acceptance criteria

1. Install and remove the UI alongside a running 0.3.3 installation. Scheduled backups still publish through the standard path in both cases.
2. With two profiles and multiple pages, verify ordering, navigation and access boundaries. Inject a manifest hash mismatch, an unmarked manifest and a deletion record. Only valid published copies appear. S3 request tracing shows no GET of artifacts/ objects on page views.
3. For logical and physical copies compare the tab to the stored note in each language, including a note whose order differs from the manifest artifact array. Opening a second run displays its own instructions. Exercise a legacy omission, a missing declared object and a hash mismatch. Each yields its specified status without synthesized steps.
4. Show distinct instants for identical cron expressions in two zones with different offsets. Allow the administrator to run a paused schedule manually. A missing CronJob prevents launch. Cron and suspend values match their originals after launch.
5. Double click, simultaneous POSTs, lost Kubernetes response, UI restart and replay after TTL create at most one Job per key. Reject connection overrides, Jobs with modified templates and launches outside profile scope. Record outcomes in the audit trail.
6. Follow a Job from pending through completion and exercise Failed and SkippedBusy separately. A successful Job without a marker has no published copy link. With a valid marker and matching UID event, navigate in both directions. Reuse of a Job name preserves identity isolation.
7. Disable S3 and the Kubernetes API in turn. Data from the healthy source stays available. Display failure reason and data age. Check out-of-scope deep links, redacted HTTP responses and safe display of database names containing HTML.
8. Exercise anonymous reads and launches, invalid credentials, successful administrator login, logout and an expired session. Rotate the Vault secret and verify that only the new credentials can establish a session and that an old session is rejected. With Vault unavailable, both login and protected requests fail closed. Check that no password appears in logs, storage outside Vault or responses.
9. With globals enabled, confirm the configured maintenance database is displayed literally, including the default when omitted. Exercise direct and staged launches against a denied globals export. Display the persisted failure reason and failed phase, with the prerequisite guidance link. Uploaded partial files yield no published copy link. Historical generic TOOL_FAILED records retain their recorded code and an unknown underlying cause.
