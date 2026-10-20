# Roadmap

The current release contains no UI/API server. A future optional UI may authenticate operators, list already configured PostgreSQL targets/profiles and start a manual backup by profile ID through the existing immutable CronJob/Job launch path. It should show run status and sanitized logs. It must not accept arbitrary connection credentials or introduce a second backup engine. Before implementation, specify authentication/authorization, profile allowlists, narrowly scoped create-Job permissions, audit records, idempotent launch requests and cancellation. Existing CLI launch/status contracts and stable profile IDs are the integration boundary; no additional RBAC is needed merely for this roadmap.

Incremental physical backups and PITR remain separate future work: versioned chain/WAL metadata, dependency-aware retention and offline restore verification. No flags promise these features in 0.2.
