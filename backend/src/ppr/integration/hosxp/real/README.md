# Real HOSxP adapter — generic, configured per site

`SqlHosxpGateway` implements `HosxpGateway` by running **read-only SQL** against the
hospital's HOSxP database (MySQL/MariaDB or PostgreSQL). See ADR-007 and
`docs/HOSXP_CONNECTION.md`.

The code here still contains **no HOSxP table names, column names or status codes**
(spec §47: they are never guessed). All site knowledge lives in the site query file
`hosxp_queries.toml` (git-ignored; template: `hosxp_queries.example.toml`), written only
after inspecting the real schema with `python -m ppr.cli hosxp-schema`.

- `query_config.py` — loads the site file; refuses anything but one SELECT/WITH per
  operation, with exactly the operation's parameters; maps native PR status → normalized.
- `sql_gateway.py` — runs each query live in a READ ONLY transaction with a statement
  timeout, checks the returned columns against the contracts; connection problems →
  `HosxpUnavailableError`, bad query/columns → `HosxpConfigurationError` (callers refuse
  in both cases).
- `schema_discovery.py` — lists table/column NAMES matching keywords; never reads rows.

Still open (needs real evidence, spec §47): HOSxP user authentication for login
(`HosxpAuthenticationAdapter`), the native PR status values, budget snapshot and stock
issue queries (not used by current features).
