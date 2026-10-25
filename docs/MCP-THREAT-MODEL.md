# MCP server threat model

Companion to [MCP.md](MCP.md). Each row names a threat, the control that
closes it and the test that proves the control. The `Status` column is the
truth about where the implementation stands: `planned`, `built` (code exists)
or `tested` (an automated test exercises the wire behaviour). A row is not
done until it reads `tested`.

Mappings refer to the MCP
[security best practices](https://modelcontextprotocol.io/specification/2026-07-28/basic/security_best_practices)
page (SBP), the
[authorization specification](https://modelcontextprotocol.io/specification/2026-07-28/basic/authorization)
(AUTH), the [OWASP MCP Top 10](https://owasp.org/www-project-mcp-top-10/)
(MCP01 to MCP10) and STRIDE letters.

## Assets

- Access, refresh and authorization-code tokens, and the grants behind them.
- The administrator session and the consent decision.
- The catalog and job data, and the ability to queue or cancel work.
- The token pepper and the session secret.
- The audit log.

## Actors

- An MCP client holding a valid grant with limited scopes.
- A client that was never granted anything.
- A party that obtained a bearer token (log leak, clipboard, a compromised
  client).
- A hostile metadata document or hostile tool input.
- Malicious content inside the catalog (file names, paths) that reaches the
  model through tool results.

## Trust boundaries

1. Client to `/mcp` over TLS: authenticated by bearer token.
2. Browser to the consent page: authenticated by the admin session.
3. `/mcp` to the repositories: authorized by the scope gate and the registry.
4. The authorization server to a metadata document URL: an outbound fetch
   guarded against SSRF.

## Threats and controls

| # | Threat | Mapping | Control | Test that proves it | Status |
|---|--------|---------|---------|---------------------|--------|
| 1 | Token forgery or substitution: a worker token, a refresh token, a guessed value or a token minted for another resource is presented as an access token | AUTH token handling; SBP token passthrough; MCP01, MCP07; S, E | One verifier: digest lookup with kind-prefixed HMAC, kind check, expiry, grant revocation, client enabled, resource equal to the canonical URL | A worker token, a refresh token, a random string, an expired token, a token under a revoked grant and a token whose grant names another resource all get `401` and zero repository calls | tested |
| 2 | Missing or malformed credential | AUTH error handling; MCP07; S | `401` with `WWW-Authenticate: Bearer resource_metadata=...` | Request without a header, with a non-bearer scheme and with an empty bearer value each get `401` and the header; the metadata URL in the header resolves to the RFC 9728 document | tested |
| 3 | Admin session used as MCP credential | SBP state handle hijacking; MCP07; S | `/mcp` never reads the session; the auth middleware passes it through to the bearer stack | A logged-in browser session with no bearer token gets `401` on `/mcp` | tested |
| 4 | Scope escalation: a read grant invokes a write tool, or a tool with no policy is reachable | SBP scope minimization; AUTH scope challenge; MCP02; E | Registry is the only source of tool policy; registration fails for a tool without scopes; gate denies unknown names; `403 insufficient_scope` names the missing scopes | Read grant calling a write tool gets `403` with `scope=` and `resource_metadata` and zero repository calls; a registry entry without scopes fails the import-time check; an unregistered name gets `403` | tested |
| 5 | Header and body disagree: the gate is told one tool, the body names another | AUTH; transport header requirements; T, E | Gate authorizes the header-named operation; the SDK rejects a mismatch before dispatch | Read header with write body is rejected before execution; write header with read body is rejected by the gate; matching write header and body gets `403`; matching read pair succeeds | tested |
| 6 | Legacy transport admitted: a session-based 2025 request bypasses the header-based gate | AUTH; transport; E | Protocol admission requires a supported modern `MCP-Protocol-Version`; the legacy path is never reached | Missing header, a 2025 revision and a duplicate header each get `400`; a `2026-07-28` request passes | tested |
| 7 | Token leaks through logs or responses | MCP01; I | Tokens and arguments are excluded from audit records and application logs; error bodies carry no token material | A canary token issued in a test never appears in captured logs or in any response body | tested |
| 8 | Replay of a consumed refresh token or authorization code | AUTH refresh tokens; OAuth 2.1 section 4.3; S | Codes are single use and consumed atomically with issuance; refresh rotates and a replay revokes the grant | Four concurrent exchanges of one code yield one success; a replayed code or refresh token returns `invalid_grant` and the grant is revoked, on SQLite and Postgres (`tests/test_mcp_authz.py`) | tested |
| 9 | Authorization code interception or redirect to an attacker | SBP confused deputy; AUTH redirect URI; RFC 8252 7.3; S | PKCE `S256` mandatory; exact redirect match with the loopback-port exception only for registered loopback URIs; `resource` required and preserved in the grant | Wrong verifier, unregistered callback, changed path on a loopback URI, missing `resource` and a `resource` naming another server are all rejected; a different loopback port on a registered loopback URI succeeds; a non-loopback URI matches only with its own port | tested |
| 10 | Consent bypass: a crafted authorize link turns an existing login into a grant | SBP confused deputy, consent requirements; S, E | Consent is a server-stored transaction approved with the admin session plus a nonce bound to that transaction; the page names the client, the scopes and the redirect URI; the page cannot be framed | Approval without the nonce, with a nonce from another transaction, or from a cross-site POST is rejected; a logged-in administrator opening the link grants nothing; the consent response carries `frame-ancestors 'none'` | tested |
| 11 | SSRF through a metadata document URL | SBP SSRF against authorization servers; I, E | Allowlisted URLs only; HTTPS only; public addresses only after resolution; redirects not followed; size and time bounds | A URL not on the allowlist, a private or link-local address, an `http://` URL, a redirecting URL and an oversized document are each refused without a second request; the one request goes to the vetted address with the hostname kept for TLS (`tests/test_mcp_cimd.py`) | tested |
| 12 | Audience moved by a hostile `Host` or forwarded header | AUTH canonical URI; SBP; T | Canonical resource comes from `TF_PUBLIC_URL`, never from request headers | Requests with a foreign `Host` or `X-Forwarded-Host` get the configured resource in metadata and the same audience check | tested |
| 13 | Tool poisoning or prompt injection through catalog content | SBP; MCP03, MCP06; T | Tool definitions are code; catalog strings are returned as data with bounded length; no tool runs commands or fetches URLs supplied by the model | A file name containing instructions comes back verbatim inside a bounded field and cannot change any grant, destination or tool set | tested |
| 14 | Data over-sharing or error leakage | MCP10; I | Allowlisted output fields; bounded page sizes; errors carry a reason, never a stack trace or path internals | Tool results contain only the documented fields; an internal exception surfaces as a generic tool error | tested |
| 15 | Repudiation: no record of who did what | MCP08; R | One audit record per tool call with grant id, client id, tool, decision, outcome and correlation id | An allowed call and a denied call both produce a record; the record contains no token | tested |
| 16 | Resource exhaustion | D | 1 MiB body cap; per-grant request budget; bounded page sizes | Oversized body gets `413`; the request after the budget gets `429`; neither reaches a repository | tested |
| 17 | Compromised dependency or image | MCP04; T | `mcp` pinned in `uv.lock`; image lock check in CI; version bumps are deliberate commits | CI `image-build` job runs `scripts/check_image_lock.py` | tested (existing gate) |
| 18 | Mode confusion between local and external issuers | AUTH; S | One issuer mode selected explicitly; the other mode's routes are not mounted and its verifier is not constructed | In external mode `/oauth/token` is `404` and a local token gets `401`; in local mode an external JWT gets `401` | planned (Phase 3) |

## Review evidence

What a reviewer should be able to pick up without asking:

- This document, with every row at `tested`, and the design record.
- The tool inventory and the scope table in the design record, generated from
  the registry rather than maintained by hand.
- The wire tests under `tests/`, runnable on SQLite and Postgres.
- The deployment contract in the design record, and the reverse proxy
  configuration used in the release check.
- The dependency lock and the image lock check output from CI.

## Assumptions and residual risk

- The installation has one administrator. Grants separate clients from each
  other; they do not separate users. Multi-user isolation is out of scope.
- A bearer token in the hands of an attacker is the attacker for one hour,
  bounded by the grant's scopes. Revocation takes effect at the next check.
- The embedded authorization server is reviewed as part of this code base.
  It exists because a self-hosted product has no external issuer by default;
  installations that have one use external issuer mode instead.
- The MCP client is trusted to keep its tokens confidential and to validate
  the authorization server's `iss` parameter.
