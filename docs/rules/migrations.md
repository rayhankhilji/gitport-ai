# Database Migration Rules

These rules exist because of real outages. Follow them or the deploy breaks prod.

## Expand-migrate-contract

Never deploy a destructive schema change in the same release as the code that
stops using it. Expand (add new shape), migrate data, contract (remove old
shape) across at least two deploys.

## Locking operations

- `ALTER TABLE ... ADD COLUMN ... NOT NULL` without a `DEFAULT` rewrites the
  entire table under an exclusive lock on Postgres < 11. Add nullable or with
  a server-side default, backfill in batches, then set NOT NULL.
- `CREATE INDEX` without `CONCURRENTLY` blocks all writes to the table. Always
  `CONCURRENTLY`, always outside a transaction.
- `ALTER COLUMN ... TYPE` rewrites the table. Create a new column, dual-write,
  backfill, swap.
- Foreign keys: add with `NOT VALID`, then `VALIDATE CONSTRAINT` separately.

## Destructive changes

- `DROP COLUMN` / `DROP TABLE` only after a full deploy cycle proves nothing
  reads it. Rename to `*_deprecated` first.
- Unqualified `UPDATE`/`DELETE` (no `WHERE`) is forbidden in migrations. Batch
  by primary key ranges.
