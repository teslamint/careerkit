# External Caller Checklist

Sanitized pre-cutover checklist for out-of-repository callers. This document records only category-level status and disposition, never shell command transcripts, arguments, credentials, private identifiers, or absolute home paths.

| Category | Status | Disposition | Notes |
|---|---|---|---|
| Personal scripts | cleared | no active legacy caller found | Standard personal script locations were checked locally; no matching active caller was found. |
| Shell aliases and habitual commands | history-only | update checked-in guidance; accept inert history | No active alias/config reference was found. Historical invocations are inert and document the habit that U6 guidance must replace. |
| Schedulers (launchd/cron/automation) | cleared | no migration required | User scheduler definitions and the current crontab contained no matching active caller. |
| Editor tasks / IDE run configs | cleared | no migration required | Repository editor-task locations contained no matching active caller. |
| Device / push automation | verified at cutover | keep as-is | A private device/push workflow exists and was rechecked immediately before U11; it has no legacy command or import dependency and survives cutover unchanged. |

## Review Contract

- Every category must have a non-empty `Status` and `Disposition`.
- This file must never store command text, shell transcripts, credentials, private file paths, company names, or home-directory identifiers.
- Unresolved or blank entries block actual cutover readiness.
