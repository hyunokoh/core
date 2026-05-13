<!-- Thanks for contributing to zkCEX. Fill in each section below. -->

## Summary

<!-- 1-3 sentences describing what this PR changes and why. -->

## Test plan

- [ ] Unit tests added/updated (`pytest tools/ -m "not integration and not live"`)
- [ ] Integration tests pass (`bash tools/run_integration_tests.sh`)
- [ ] Live zkPoL E2E considered for proof/anchor changes (`zkpol-live-e2e.yml`)
- [ ] Manual smoke against staging (if user-facing)
- [ ] No regressions in lint / type checks (`ruff check tools/`, `mypy tools/`)

## Security review

<!--
Required for changes touching:
  - auth_server.py / auth_db.py / api_key_server.py
  - custody/ / travel_rule/
  - waf.py
  - any new external dependency
Describe the threat model touched and which mitigations are in place.
-->

- [ ] No new secrets committed
- [ ] No new third-party action without owner sign-off
- [ ] Touches sensitive paths? If yes, @security-team review requested

## Migration steps

<!--
If this PR introduces a DB migration, config schema change, or breaks
backward compatibility, list the steps an operator must take in order.
Otherwise: "None."
-->

## Rollback plan

<!--
How to roll back if this lands in staging or production and causes an
incident. Be specific (helm rollback revision, feature flag toggle, etc.).
-->

## Linked issue

Closes #
