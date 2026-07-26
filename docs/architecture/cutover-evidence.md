# Repository Cutover Evidence

Point-in-time, metadata-only evidence for the U9 cutover gate. Private configuration
values, document bodies, company names, record identities, command history, and
backup names are intentionally excluded.

## 2026-07-14 U9 local gate

| Check | Result |
|---|---|
| Configuration apply | Operator approved and executed; normalized rewrite completed |
| Post-apply configuration check | `ready=true`, `action=noop`, normalized role `backend`, zero findings |
| Canonical storage preflight | `ready=true`, schema version `1`, 1,562 records, 1,504 screenings, zero findings |
| Rollback artifacts | Data and permission metadata retained as regular files; maximum mode `0600` |
| External caller checklist | No unresolved category; private device/push workflow verified independent of legacy commands |
| U11 entry recheck | Device/push category still present with one workflow and no legacy dependency |

This evidence unblocks U11 at the recorded point in time. U11 must recheck the
external-caller category again if the deletion boundary changes. U8 must repeat
both read-only local preflights after the final installed-distribution cutover.

## 2026-07-14 U8 final recheck

| Check | Result |
|---|---|
| Configuration check | `ready=true`, `action=noop`, normalized role `backend`, zero findings |
| Canonical storage preflight | `ready=true`, schema version `1`, 1,562 records, 1,504 screenings, zero findings |
| External caller checklist | All categories resolved; device/push category rechecked at the U11 boundary |
| Built distributions | Fresh wheel and sdist passed allowlist and privacy-sentinel checks |
| Installed commands | Both public CLIs passed from outside the checkout against synthetic data |
| Container proof | Application and pinned-render images built and ran with Podman using installed package surfaces |
| Render equivalence | Pinned-container baseline passed; host-only pixel comparison skipped because host tool versions differ |

The final recheck was read-only for actual private state. It did not apply or
restore configuration, rebuild actual derived data, or emit document bodies,
identities, configuration values, or external command text.
