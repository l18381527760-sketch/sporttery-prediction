# Opportunity-Gated Email Design

**Date:** 2026-07-24

## Goal

Send the daily report only when the Beijing business date contains at least one
actionable simulated betting-plan candidate or at least one published draw
alert. Keep no-opportunity days silent. Extend the current cloud retry window
through 21:00 Asia/Shanghai without allowing stale, unverified, duplicate, or
real-money email behavior.

## Confirmed Product Rules

- A report is eligible when either the actionable plan count is greater than
  zero or the draw-alert count is greater than zero.
- An actionable plan is an active simulated candidate from the signed
  provisional state. Shadow candidates, cancelled candidates, historical rows,
  and real-money activity do not count.
- A draw alert is a valid same-date row emitted by the draw-alert generator.
  Observation-stage alerts count because they are published warnings, even when
  their additional simulated stake is zero.
- When both counts are known and equal to zero, the day is silent: no normal
  report and no no-opportunity notice.
- When the opportunity state cannot be proven, the system fails closed and
  sends no email.
- Normal report delivery starts at 14:00 and must occur before 21:00
  Asia/Shanghai. The existing ten-minute trigger remains in place.
- When an opportunity is proven but the verified report is still unavailable at
  21:00, send one attachment-free failure notice.
- At 21:00, an absent or unknown opportunity state remains silent.
- Revalidation update email is allowed only after an initial opportunity report
  was sent for the same business date and only before that date's 21:00 cutoff.
  Later changes remain available on the website and in the audit artifacts.

## Approaches Considered

### 1. Publish opportunity state in the report-status contract

The report producer validates the authoritative artifacts and publishes a
machine-readable opportunity block. Apps Script validates that block before
fetching the image or sending mail.

This is the selected approach because it gives one auditable decision point,
keeps artifact parsing in Python, and makes the Apps Script sender small and
fail-closed.

### 2. Let Apps Script download and count both CSV files

This adds independent cache-busted downloads, CSV parsing, and cross-file
freshness checks to Apps Script. It increases the chance of observing a partial
deployment and duplicates Python validation rules.

### 3. Infer opportunity from website text or the report image

This couples delivery to presentation copy and layout. It is rejected because a
wording or rendering change could cause a false send.

## Status Contract

The producer advances the report-status schema and publishes:

```json
{
  "email_opportunity": {
    "state": "present",
    "actionable_plan_count": 1,
    "draw_alert_count": 0,
    "reasons": []
  }
}
```

`state` has exactly three values:

- `present`: at least one authoritative source is valid and has a positive
  count.
- `absent`: both authoritative sources are valid and both counts are zero.
- `unknown`: neither a positive opportunity nor a proven all-zero day can be
  established.

Positive evidence is sufficient under the confirmed OR rule. For example, a
valid actionable plan count of one proves `present` even if the draw-alert
artifact is temporarily unavailable. `absent` requires both sources to be valid
and zero.

Counts are non-negative integers when their source is valid and `null` when it
is not. `reasons` contains stable diagnostic codes for invalid or unavailable
sources and is empty for a fully proven state.

## Producer Validation

### Actionable plan source

Use the signed, date-scoped provisional state already validated by
`read_valid_provisional_state`. Count only `active_candidate_count`. Do not add
`shadow_candidate_count`.

### Draw-alert source

Read `output/draw_alert_<date>.csv`. A valid artifact must:

- exist and contain the complete expected header;
- contain only rows whose `date` equals the report date;
- contain structurally valid non-negative ranks and non-empty match identity,
  subtype, selection, and strategy fields;
- treat a header-only file as a valid zero-alert result.

An invalid or missing file contributes an unknown count, not zero.

The opportunity block is derived afresh for every report-status phase. A prior
day's block or an earlier phase's positive count cannot be carried across a
different business date.

## Apps Script Sending Rules

Apps Script accepts only the new supported report-status schema and validates
the opportunity block independently:

- counts must be integer or `null` as required by the state;
- `present` must have at least one positive count;
- `absent` must have two zero counts;
- `unknown` never permits mail;
- malformed or contradictory blocks are treated as unknown.

Before 14:00, the orchestrator may dispatch required workflows but does not
send the daily report. From 14:00 through 20:59:

- `present` plus the existing full readiness and image-hash checks sends one
  normal report;
- `absent` sends nothing;
- `unknown` sends nothing and allows normal workflow retries to continue.

At or after 21:00:

- `present` with no recorded initial send produces one attachment-free failure
  notice that reports the missed delivery deadline and any readiness reasons;
- `absent` and `unknown` send nothing;
- no normal report is sent after the deadline.

The dispatch cutoff moves from 18:00 to 21:00 so a delayed producer can recover
inside the expanded delivery window. Existing lock, cooldown, image SHA-256,
same-date, settlement, data-quality, and deduplication checks remain mandatory.

## Revalidation Updates

Revalidation dispatch may continue according to its evidence window, but email
delivery is filtered separately. An update is eligible only when:

- the initial report was sent for the same report date;
- the report date equals the current Beijing date;
- the Beijing clock is earlier than 21:00;
- the immutable revision image and digest pass existing checks.

Expired updates are retained in published evidence but are no longer queued for
email.

## Failure Behavior

- Proven opportunity plus report/image failure at 21:00: one failure email,
  without an attachment.
- Proven no-opportunity day: silent.
- Missing, stale, malformed, contradictory, or partial opportunity evidence:
  silent.
- Gmail failure: do not write sent-state. A normal-report retry is allowed only
  before 21:00; a failure-notice retry remains deduplicated and may occur on a
  later trigger after the cutoff.
- Duplicate trigger execution: existing lock and date-scoped sent-state prevent
  duplicate email.

## Tests

Python tests will prove:

- active plan only produces `present`;
- shadow plan only does not produce `present`;
- draw alert only produces `present`;
- both positive produces `present`;
- two valid header-only/zero sources produce `absent`;
- either source missing with the other valid and zero produces `unknown`;
- positive evidence from either valid source overrides the other source being
  unavailable;
- cross-date or malformed draw-alert rows are rejected;
- generated status uses the new schema and never carries stale opportunity
  state.

Apps Script tests will prove:

- plan-only, alert-only, and both-positive statuses send;
- absent and unknown statuses never send;
- contradictory opportunity blocks fail closed;
- 13:59 does not send;
- 20:59 may send a ready report;
- 21:00 never sends a normal report;
- 21:00 sends one failure notice only for a proven opportunity;
- no-opportunity and unknown days receive no failure notice;
- workflow retries continue until the new cutoff;
- revalidation updates are suppressed at and after 21:00 and for prior dates;
- hash mismatch, stale date, Gmail failure, and duplicate-trigger protections
  continue to work.

Documentation tests will require README, cloud setup, and Apps Script deployment
instructions to describe the 14:00-21:00 opportunity-gated behavior and remove
the former unconditional 18:00 failure-notice rule.

## Deployment

1. Merge and publish the producer, tests, documentation, and Apps Script source
   together.
2. Update the existing Apps Script project with the committed `Code.gs`.
3. Run in `TEST_MODE=true` with one present fixture and one absent fixture.
4. Set `TEST_MODE=false` only after the logs show correct opportunity state,
   cutoff handling, and no Gmail call for the absent fixture.
5. Observe one live Beijing business date. The system remains simulation-only
   and does not connect to a betting account.

## Acceptance Criteria

- A plan-only day sends one verified report before 21:00.
- A draw-alert-only day sends one verified report before 21:00.
- A day with neither sends no email of any kind.
- An unknown opportunity state sends no email.
- A proven-opportunity report failure sends one attachment-free notice at
  21:00.
- No normal or revalidation email for that report date is sent at or after
  21:00.
- Existing audit, settlement, simulation-budget, and no-real-money guarantees
  remain unchanged.
