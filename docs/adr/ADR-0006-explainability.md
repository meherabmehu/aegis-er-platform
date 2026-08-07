# ADR-0006 — Explainability as a first-class artifact

## Status
Accepted.

## Context
Human EOC commanders must trust and occasionally override dispatch decisions; regulators require audit trails; post-incident reviews need to understand why a given unit was or wasn't sent.

## Decision
Every Dispatch carries a `rationale` JSON field including the chosen unit, the top 5 candidates considered with their ETA and rejection reason, the active constraints, the objective weight breakdown, the optimality gap, and a plain-text summary. A dedicated explainability service generates these consistently.

## Consequences
- **+** Operator trust and adoption.
- **+** Clean audit trail for legal/regulatory review.
- **+** Excellent debugging signal when the solver behaves unexpectedly.
- **−** Small storage overhead (a few hundred bytes per dispatch) — negligible given dispatch volume.
