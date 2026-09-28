# API Compatibility Rules

External clients are pinned to our API for months. Breaking them is a
production incident, not a refactor.

## Never in a minor release

- Removing or renaming a field from a response schema.
- Changing a field's type or its nullability.
- Adding a required parameter to an existing endpoint.
- Changing HTTP status codes clients may depend on (2xx -> 4xx).

## Safe changes

- Adding a new optional field.
- Adding a new endpoint under /v1/.
- Deprecating a field while still populating it.

## If you must break

Ship it under /v2/, keep /v1/ alive, and write a migration note in the
changelog. The old path gets a sunset header, not a deletion.
