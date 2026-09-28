# Post-mortem: 2024-05 sessions table outage

## What happened

A migration shipped `DELETE FROM sessions;` without a WHERE clause inside the
same transaction as a schema change. The delete took a row lock on every row
and held it; checkout latency spiked to 30s and the sessions table backed up
for 19 minutes. Revenue impact: ~$210k.

## Root cause

The PR looked small. Nobody realized the unqualified DELETE would scan and
lock 40M rows, and the DDL in the same transaction extended the lock window.

## Prevention

- Unqualified DELETE/UPDATE in migrations is now a hard gate failure.
- DDL and DML must ship in separate migrations.
- Any migration touching a table over 1M rows needs a runtime estimate in the
  PR description.
