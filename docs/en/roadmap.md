# Roadmap

The draft [standalone UI v1 specification](ui-v1.md) covers S3 catalog browsing, recovery order, schedules, one-off launches and Job monitoring.

The optional console is implemented as a separate image and chart; see [UI deployment and local validation](ui-operations.md). Log streaming, cancellation, archive downloads, restore/verify launches and profile/schedule editing remain outside v1.

Incremental physical backups and PITR remain separate future work: versioned chain/WAL metadata, dependency-aware retention and offline restore verification. No flags promise these features in 0.2.
