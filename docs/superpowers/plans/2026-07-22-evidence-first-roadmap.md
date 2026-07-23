# Evidence-First Forecasting Roadmap

**Source specification:** `docs/superpowers/specs/2026-07-22-evidence-first-forecasting-system-design.md`

## Delivery Order

The confirmed design is split into four independently reviewable projects. Each project must be complete, tested, deployed in shadow mode, and reviewed before the next project changes its interfaces.

1. **Data Evidence Foundation**
   - Preserve historical fixture identity from immutable import manifests.
   - Promote verified fallback results to canonical 90-minute results.
   - Repair training eligibility and pre-kickoff snapshot coverage.
   - Publish machine-readable evidence health.
   - Detailed plan: `docs/superpowers/plans/2026-07-22-data-evidence-foundation.md`

2. **Independent Probability Model**
   - Bind historical features into immutable prediction inputs.
   - Remove same-day market seeding from team strength.
   - Separate structural probabilities, de-vig market baseline, fusion, calibration, and uncertainty.
   - Add chronological train/calibration/shadow evaluation.
   - Planning checkpoint: begins after Project 1 interfaces pass tests and seven consecutive production-shaped dry runs; the 30-day acceptance window continues in parallel.

3. **Value Portfolio and Risk**
   - Introduce `evidence-v1` no-bet reasons and conservative EV gates.
   - Separate core financial rows, zero-stake draw-alert observations, and the daily 30-yuan experiment.
   - Add 0.10 Kelly, 750-yuan reduction, and 1250-yuan monthly pause.
   - Reuse the existing monotonic T-90/T-30 revalidation system.
   - Planning checkpoint: begins after Project 2 produces frozen calibrated probabilities and uncertainty.

4. **Reporting and Operations**
   - Build an evidence-first website and email view model.
   - Isolate legacy and current strategy metrics.
   - Keep Apps Script as the sole sender and enforce report revision idempotency.
   - Add 30-day operational acceptance and alerting.
   - Planning checkpoint: begins after Projects 1-3 expose stable read models.

## Release Gates

- Every project uses test-driven changes and small commits.
- Existing `value-v4` remains shadow-only during Projects 1-2.
- `real_money_automation` remains `false` in every project.
- A failed evidence gate produces zero new simulated paid rows.
- Historical artifacts and legacy ledger economics are never rewritten.
- The next project cannot start merely because code merged; its predecessor's acceptance tests must pass on production-shaped fixtures.

## Specification Coverage

| Specification sections | Owning project |
|---|---|
| 7-9 identity, immutable evidence, data sources; 16 settlement | Project 1 |
| 10-12 probability model, draw layer, chronological model governance | Project 2 |
| 13-15 value, portfolio, money management, revalidation | Project 3 |
| 17-24 automation, workflows, site, email, monitoring, testing, migration, acceptance | Project 4, with Project 1 delivering the shared evidence-health prerequisites |
| 1-6 goals, non-goals, audit baseline, principles, architecture | Global constraints enforced by every project |
| 25 confirmed decisions | Release checklist enforced by every project |

## Why Plans 2-4 Are Deferred

Their exact function signatures depend on the canonical identity, result eligibility, snapshot phase, and health-report interfaces delivered by Project 1. Writing executable code-level plans before those contracts are reviewed would either duplicate work or freeze incorrect assumptions. Each later plan will follow the same TDD format as Project 1 and will be written at its checkpoint without changing the confirmed specification.
