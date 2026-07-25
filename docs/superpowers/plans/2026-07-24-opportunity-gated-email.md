# Opportunity-Gated Email Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Send the verified daily report before 21:00 Asia/Shanghai only when a same-date actionable simulated plan or published draw alert exists.

**Architecture:** `report_status.py` remains the authoritative artifact validator and publishes a three-state `email_opportunity` contract. `apps-script/Code.gs` validates that contract, preserves all existing report and image integrity checks, and applies the 14:00-21:00 send window without parsing CSV or HTML itself.

**Tech Stack:** Python 3 standard library and `unittest`, Google Apps Script JavaScript, Node.js built-in test runner, GitHub Actions documentation tests.

## Global Constraints

- Opportunity uses OR semantics: actionable plan count greater than zero or draw-alert count greater than zero.
- Count only signed active provisional candidates; never count shadow candidates, cancelled candidates, historical rows, or real-money activity.
- Count valid same-date draw-alert rows, including zero-stake observation alerts.
- `absent` requires two valid zero sources; missing or malformed evidence is `unknown`.
- Normal mail is allowed from 14:00 through 20:59 Asia/Shanghai and never at or after 21:00.
- At or after 21:00, send one attachment-free failure notice only when `present` was proven and no initial report was sent.
- `absent` and `unknown` produce no email.
- Revalidation email requires a same-date initial send and a clock earlier than 21:00.
- Keep simulation-only behavior, existing SHA-256 checks, date checks, locks, cooldowns, and deduplication.
- Do not add dependencies.

---

### Task 1: Publish authoritative opportunity state

**Files:**
- Modify: `report_status.py`
- Modify: `tests/test_report_status.py`

**Interfaces:**
- Consumes: `read_valid_provisional_state(root, report_date)` and `output/draw_alert_<date>.csv`.
- Produces: `_draw_alert_artifact(path: Path, report_date: date) -> tuple[bool, int]`.
- Produces: `_email_opportunity(state: dict) -> dict`.
- Produces in status: `email_opportunity = {"state": str, "actionable_plan_count": int | None, "draw_alert_count": int | None, "reasons": list[str]}`.

- [ ] **Step 1: Write failing draw-alert artifact tests**

Add `DRAW_ALERT_FIELDS` in `tests/test_report_status.py` with the exact generator
header and add tests that exercise the wished-for helper:

```python
from report_status import _draw_alert_artifact, _email_opportunity

DRAW_ALERT_FIELDS = [
    "date", "rank", "match_id", "match", "team_a", "team_b", "stage",
    "subtype", "selection", "domestic_draw_odds",
    "market_draw_probability", "model_draw_probability", "draw_edge",
    "expected_value", "xg_total", "global_calibrated_draw_probability",
    "league_calibration_samples", "league_calibration_enabled",
    "evidence_json", "data_quality", "captured_at", "alert_level",
    "additional_stake", "linked_main_stake", "hypothetical_stake",
    "settlement_mode", "strategy_version", "feature_version",
]

def draw_alert_row(self, **overrides) -> dict:
    row = {field: "value" for field in DRAW_ALERT_FIELDS}
    row.update({
        "date": REPORT_DATE.isoformat(),
        "rank": "1",
        "match_id": "001",
        "match": "A vs B",
        "team_a": "A",
        "team_b": "B",
        "subtype": "balanced_draw",
        "selection": "draw",
        "strategy_version": "draw-v1",
    })
    row.update(overrides)
    return row

def write_draw_alert(self, root: Path, rows: list[dict]) -> Path:
    path = root / "output" / f"draw_alert_{REPORT_DATE.isoformat()}.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=DRAW_ALERT_FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    return path

def test_draw_alert_artifact_accepts_header_only_as_known_zero(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "output").mkdir()
        path = self.write_draw_alert(root, [])
        self.assertEqual((True, 0), _draw_alert_artifact(path, REPORT_DATE))

def test_draw_alert_artifact_counts_valid_same_date_rows(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "output").mkdir()
        path = self.write_draw_alert(root, [self.draw_alert_row()])
        self.assertEqual((True, 1), _draw_alert_artifact(path, REPORT_DATE))

def test_draw_alert_artifact_rejects_cross_date_and_malformed_rows(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "output").mkdir()
        path = self.write_draw_alert(
            root, [self.draw_alert_row(date="2026-07-15")]
        )
        self.assertEqual((False, 0), _draw_alert_artifact(path, REPORT_DATE))
        path = self.write_draw_alert(
            root, [self.draw_alert_row(match_id="")]
        )
        self.assertEqual((False, 0), _draw_alert_artifact(path, REPORT_DATE))
```

- [ ] **Step 2: Run the focused tests and verify RED**

Run:

```powershell
python -m unittest tests.test_report_status.ReportStatusTest.test_draw_alert_artifact_accepts_header_only_as_known_zero tests.test_report_status.ReportStatusTest.test_draw_alert_artifact_counts_valid_same_date_rows tests.test_report_status.ReportStatusTest.test_draw_alert_artifact_rejects_cross_date_and_malformed_rows -v
```

Expected: FAIL because `_draw_alert_artifact` is not defined.

- [ ] **Step 3: Implement strict draw-alert validation**

Add the exact generator header as `DRAW_ALERT_REQUIRED_FIELDS`, parse with
`csv.DictReader`, and fail closed:

```python
def _draw_alert_artifact(path: Path, report_date: date) -> tuple[bool, int]:
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            if not reader.fieldnames or not DRAW_ALERT_REQUIRED_FIELDS.issubset(reader.fieldnames):
                return False, 0
            rows = list(reader)
    except OSError:
        return False, 0
    for row in rows:
        if row.get("date") != report_date.isoformat():
            return False, 0
        try:
            rank = int(row.get("rank", ""))
        except (TypeError, ValueError):
            return False, 0
        if rank < 1 or any(not str(row.get(field, "")).strip() for field in (
            "match_id", "match", "team_a", "team_b", "subtype", "selection",
            "strategy_version",
        )):
            return False, 0
    return True, len(rows)
```

- [ ] **Step 4: Run the focused tests and verify GREEN**

Run the Step 2 command.

Expected: PASS.

- [ ] **Step 5: Write failing three-state opportunity tests**

Add table-driven assertions:

```python
def test_email_opportunity_uses_three_state_or_semantics(self):
    cases = [
        ({"provisional_state_ready": True, "provisional_plan_count": 1,
          "draw_alert_ready": False, "draw_alert_count": 0},
         {"state": "present", "actionable_plan_count": 1, "draw_alert_count": None, "reasons": ["draw_alert_unavailable"]}),
        ({"provisional_state_ready": False, "provisional_plan_count": 0,
          "draw_alert_ready": True, "draw_alert_count": 1},
         {"state": "present", "actionable_plan_count": None, "draw_alert_count": 1, "reasons": ["actionable_plan_unavailable"]}),
        ({"provisional_state_ready": True, "provisional_plan_count": 0,
          "draw_alert_ready": True, "draw_alert_count": 0},
         {"state": "absent", "actionable_plan_count": 0, "draw_alert_count": 0, "reasons": []}),
        ({"provisional_state_ready": True, "provisional_plan_count": 0,
          "draw_alert_ready": False, "draw_alert_count": 0},
         {"state": "unknown", "actionable_plan_count": 0, "draw_alert_count": None, "reasons": ["draw_alert_unavailable"]}),
    ]
    for state, expected in cases:
        with self.subTest(expected=expected["state"]):
            self.assertEqual(expected, _email_opportunity(state))

def test_shadow_candidates_never_make_email_opportunity_present(self):
    state = {
        "provisional_state_ready": True,
        "provisional_plan_count": 0,
        "provisional_shadow_count": 4,
        "draw_alert_ready": True,
        "draw_alert_count": 0,
    }
    self.assertEqual("absent", _email_opportunity(state)["state"])
```

- [ ] **Step 6: Run the opportunity tests and verify RED**

Run:

```powershell
python -m unittest tests.test_report_status.ReportStatusTest.test_email_opportunity_uses_three_state_or_semantics tests.test_report_status.ReportStatusTest.test_shadow_candidates_never_make_email_opportunity_present -v
```

Expected: FAIL because `_email_opportunity` is not defined.

- [ ] **Step 7: Implement opportunity derivation**

Add:

```python
def _email_opportunity(state: dict) -> dict:
    plan_ready = state.get("provisional_state_ready") is True
    alert_ready = state.get("draw_alert_ready") is True
    plan_count = state.get("provisional_plan_count") if plan_ready else None
    alert_count = state.get("draw_alert_count") if alert_ready else None
    reasons = []
    if not plan_ready:
        reasons.append("actionable_plan_unavailable")
    if not alert_ready:
        reasons.append("draw_alert_unavailable")
    if (isinstance(plan_count, int) and plan_count > 0) or (
        isinstance(alert_count, int) and alert_count > 0
    ):
        opportunity_state = "present"
    elif plan_ready and alert_ready and plan_count == 0 and alert_count == 0:
        opportunity_state = "absent"
    else:
        opportunity_state = "unknown"
    return {
        "state": opportunity_state,
        "actionable_plan_count": plan_count,
        "draw_alert_count": alert_count,
        "reasons": reasons,
    }
```

Populate `draw_alert_ready` and `draw_alert_count` in `artifact_state`, publish
`email_opportunity` on every phase, and increment `SCHEMA_VERSION` from `2` to
`3`. Do not use `plan_count` or `provisional_shadow_count` for the actionable
count.

- [ ] **Step 8: Add status integration tests**

Extend `make_artifacts` to create a header-only valid draw-alert CSV. Add tests
that publish:

```python
def test_published_status_contains_absent_opportunity_for_two_known_zero_sources(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        self.make_artifacts(root)
        state = artifact_state(root, REPORT_DATE)
        state.update({
            "provisional_state_ready": True,
            "provisional_plan_count": 0,
            "provisional_shadow_count": 0,
            "draw_alert_ready": True,
            "draw_alert_count": 0,
        })
        with patch("report_status.artifact_state", return_value=state):
            status = self.publish(root, "forecast")
    self.assertEqual(3, status["schema_version"])
    self.assertEqual("absent", status["email_opportunity"]["state"])

def test_published_status_counts_active_candidates_but_not_shadow_candidates(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        self.make_artifacts(root)
        state = artifact_state(root, REPORT_DATE)
        state.update({
            "provisional_state_ready": True,
            "provisional_plan_count": 1,
            "provisional_shadow_count": 3,
            "draw_alert_ready": True,
            "draw_alert_count": 0,
        })
        with patch("report_status.artifact_state", return_value=state):
            status = self.publish(root, "forecast")
    self.assertEqual(1, status["email_opportunity"]["actionable_plan_count"])
    self.assertEqual("present", status["email_opportunity"]["state"])

def test_published_status_does_not_carry_prior_date_opportunity(self):
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        self.make_artifacts(root)
        (root / "web" / "report-status.json").write_text(
            json.dumps({
                **base_status(REPORT_DATE),
                "email_opportunity": {
                    "state": "present",
                    "actionable_plan_count": 4,
                    "draw_alert_count": 0,
                    "reasons": [],
                },
            }),
            encoding="utf-8",
        )
        state = artifact_state(root, REPORT_DATE)
        state.update({
            "provisional_state_ready": True,
            "provisional_plan_count": 0,
            "provisional_shadow_count": 0,
            "draw_alert_ready": True,
            "draw_alert_count": 0,
        })
        with patch("report_status.artifact_state", return_value=state):
            status = self.publish(root, "forecast")
    self.assertEqual("absent", status["email_opportunity"]["state"])
```

- [ ] **Step 9: Run report-status tests**

Run:

```powershell
python -m unittest tests.test_report_status -v
```

Expected: all tests PASS.

- [ ] **Step 10: Commit Task 1**

```powershell
git add report_status.py tests/test_report_status.py
git commit -m "feat: publish email opportunity state"
```

---

### Task 2: Gate Apps Script delivery and enforce the 21:00 cutoff

**Files:**
- Modify: `apps-script/Code.gs`
- Modify: `tests/apps_script_orchestrator.test.mjs`

**Interfaces:**
- Consumes: report-status schema 3 and `email_opportunity`.
- Produces: `emailOpportunity_(status) -> {state: string, reasons: string[]}`.
- Changes: normal window to `[14:00, 21:00)` and workflow dispatch cutoff to 21:00.
- Changes: `pendingRevalidationEmails_(index, state, clock)` filters expired and prior-date updates.

- [ ] **Step 1: Write failing opportunity-contract tests**

Change `readyStatus` and `dispatchStatus` to schema 3. Make `readyStatus`
default to:

```javascript
email_opportunity: {
  state: "present",
  actionable_plan_count: 1,
  draw_alert_count: 0,
  reasons: [],
},
```

Add:

```javascript
test("emailOpportunity_ validates present absent and unknown states", () => {
  const { context } = makeHarness();
  assert.equal(context.emailOpportunity_(readyStatus()).state, "present");
  assert.equal(context.emailOpportunity_(readyStatus({
    email_opportunity: {
      state: "absent",
      actionable_plan_count: 0,
      draw_alert_count: 0,
      reasons: [],
    },
  })).state, "absent");
  assert.equal(context.emailOpportunity_(readyStatus({
    email_opportunity: {
      state: "unknown",
      actionable_plan_count: 0,
      draw_alert_count: null,
      reasons: ["draw_alert_unavailable"],
    },
  })).state, "unknown");
});

test("emailOpportunity_ fails closed for contradictory or malformed blocks", () => {
  const { context } = makeHarness();
  const invalid = [
    undefined,
    { state: "present", actionable_plan_count: 0, draw_alert_count: 0, reasons: [] },
    { state: "absent", actionable_plan_count: 1, draw_alert_count: 0, reasons: [] },
    { state: "unknown", actionable_plan_count: -1, draw_alert_count: null, reasons: [] },
  ];
  invalid.forEach((email_opportunity) => {
    assert.equal(context.emailOpportunity_(readyStatus({ email_opportunity })).state, "unknown");
  });
});
```

- [ ] **Step 2: Run the Node tests and verify RED**

Run:

```powershell
node --test tests/apps_script_orchestrator.test.mjs
```

Expected: FAIL because `emailOpportunity_` is not defined and the script still
accepts schema 2.

- [ ] **Step 3: Implement strict Apps Script contract validation**

Add helpers that accept only non-negative integers or `null`, exact state/count
relationships, and an array of non-empty string reasons:

```javascript
function emailOpportunity_(status) {
  var block = status && status.email_opportunity;
  var invalid = { state: "unknown", reasons: ["email opportunity invalid"] };
  if (!block || typeof block !== "object" || Array.isArray(block) ||
      ["present", "absent", "unknown"].indexOf(block.state) === -1 ||
      !Array.isArray(block.reasons) ||
      !block.reasons.every(function (reason) {
        return typeof reason === "string" && Boolean(reason.trim());
      })) return invalid;
  var plan = block.actionable_plan_count;
  var alert = block.draw_alert_count;
  var validCount = function (value) {
    return value === null || integerAtLeast_(value, 0);
  };
  if (!validCount(plan) || !validCount(alert)) return invalid;
  if (block.state === "present" && !((plan !== null && plan > 0) || (alert !== null && alert > 0))) return invalid;
  if (block.state === "absent" && !(plan === 0 && alert === 0 && block.reasons.length === 0)) return invalid;
  if (block.state === "unknown" && plan !== null && alert !== null) return invalid;
  return { state: block.state, reasons: block.reasons.slice() };
}
```

Require schema 3 in `missingReasons_`, `chooseDispatch_`, and test fixtures.

- [ ] **Step 4: Write failing send-gate tests**

Add:

```javascript
test("plan-only and draw-alert-only opportunities each send once", () => {
  for (const email_opportunity of [
    { state: "present", actionable_plan_count: 1, draw_alert_count: 0, reasons: [] },
    { state: "present", actionable_plan_count: 0, draw_alert_count: 1, reasons: [] },
  ]) {
    const { context, calls } = makeHarness({
      now: "2026-07-16T06:00:00.000Z",
      status: readyStatus({ email_opportunity }),
    });
    context.runAutomation();
    assert.equal(calls.mail.length, 1);
  }
});

test("absent and unknown opportunities never fetch the image or send", () => {
  const blocks = [
    {
      state: "absent",
      actionable_plan_count: 0,
      draw_alert_count: 0,
      reasons: [],
    },
    {
      state: "unknown",
      actionable_plan_count: 0,
      draw_alert_count: null,
      reasons: ["draw_alert_unavailable"],
    },
  ];
  for (const email_opportunity of blocks) {
    const { context, calls } = makeHarness({
      now: "2026-07-16T06:00:00.000Z",
      status: readyStatus({ email_opportunity }),
    });
    context.runAutomation();
    assert.equal(calls.mail.length, 0);
    assert.equal(calls.fetch.some((call) => call.url.includes("daily-report.png")), false);
  }
});

test("positive opportunity sends even when the other source is unavailable", () => {
  const { context, calls } = makeHarness({
    now: "2026-07-16T06:00:00.000Z",
    status: readyStatus({
      email_opportunity: {
        state: "present",
        actionable_plan_count: 1,
        draw_alert_count: null,
        reasons: ["draw_alert_unavailable"],
      },
    }),
  });
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
});
```

- [ ] **Step 5: Run the Node tests and verify RED**

Run the Step 2 command.

Expected: FAIL because `runAutomation` does not gate image fetching and mail on
the opportunity state.

- [ ] **Step 6: Implement the normal-mail opportunity gate**

In `runAutomation`, compute `var opportunity = emailOpportunity_(status);`.
Between 14:00 and 20:59, call `tryVerifiedSend_` only when:

```javascript
opportunity.state === "present" &&
status &&
revalidationIndex &&
revalidationCoversReport
```

Do not fetch the image for `absent` or `unknown`. Add opportunity validation to
`missingReasons_` so a malformed block cannot pass a direct
`reportReadiness_` call.

- [ ] **Step 7: Write failing cutoff and failure-notice tests**

Replace the old unconditional 18:00 cases with:

```javascript
test("20:59 may send a ready present report", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T12:59:00.000Z" });
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][3].attachments.length, 1);
});

test("21:00 never sends a normal report", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T13:00:00.000Z" });
  context.runAutomation();
  assert.equal(calls.mail[0][3]?.attachments, undefined);
});

test("21:00 sends one failure notice only for a proven opportunity", () => {
  const present = makeHarness({ now: "2026-07-16T13:00:00.000Z" });
  present.context.runAutomation();
  present.context.runAutomation();
  assert.equal(present.calls.mail.length, 1);
  assert.equal(present.calls.mail[0][3]?.attachments, undefined);
  assert.equal(present.properties.get("LAST_FAILURE_NOTICE_DATE"), REPORT_DATE);

  for (const email_opportunity of [
    { state: "absent", actionable_plan_count: 0, draw_alert_count: 0, reasons: [] },
    { state: "unknown", actionable_plan_count: 0, draw_alert_count: null, reasons: ["draw_alert_unavailable"] },
  ]) {
    const harness = makeHarness({
      now: "2026-07-16T13:00:00.000Z",
      status: readyStatus({ email_opportunity }),
    });
    harness.context.runAutomation();
    assert.equal(harness.calls.mail.length, 0);
    assert.equal(harness.properties.has("LAST_FAILURE_NOTICE_DATE"), false);
  }
});

test("workflow dispatch retries stop at 21:00 instead of 18:00", () => {
  const incomplete = dispatchStatus({ forecast_ready: false });
  const before = makeHarness({
    now: "2026-07-16T12:59:00.000Z",
    status: incomplete,
  });
  before.context.runAutomation();
  assert.equal(before.calls.fetch.some((call) => call.url.includes("api.github.com")), true);

  const cutoff = makeHarness({
    now: "2026-07-16T13:00:00.000Z",
    status: incomplete,
  });
  cutoff.context.runAutomation();
  assert.equal(cutoff.calls.fetch.some((call) => call.url.includes("api.github.com")), false);
});
```

- [ ] **Step 8: Run the Node tests and verify RED**

Run the Step 2 command.

Expected: FAIL on the old 18:00 cutoff and unconditional failure behavior.

- [ ] **Step 9: Implement the 21:00 boundary**

Introduce:

```javascript
var NORMAL_SEND_START_MINUTES_ = 14 * 60;
var EMAIL_CUTOFF_MINUTES_ = 21 * 60;
```

Use `EMAIL_CUTOFF_MINUTES_` in `chooseDispatch_`. At or after the cutoff:

```javascript
if (clock.minutes >= EMAIL_CUTOFF_MINUTES_) {
  if (opportunity.state === "present") {
    sendFailureNotice_(
      properties,
      clock,
      uniqueReasons_(fetched.reasons.concat(["normal report missed 21:00 cutoff"])),
      status
    );
  }
  return;
}
```

Update the failure subject/body from 18:00 to 21:00. Do not call
`tryVerifiedSend_` in the cutoff branch.

- [ ] **Step 10: Write failing revalidation cutoff tests**

Update the helper signature and add:

```javascript
test("pending revalidation email requires current date and pre-cutoff clock", () => {
  const { context } = makeHarness();
  const state = {
    LAST_INITIAL_SENT_DATE: REPORT_DATE,
    SENT_REVALIDATION_DIGESTS: "[]",
  };
  assert.equal(
    context.pendingRevalidationEmails_(revalidationFixture(REPORT_DATE).index, state, clockAt(20, 59)).length,
    1
  );
  assert.equal(
    context.pendingRevalidationEmails_(revalidationFixture(REPORT_DATE).index, state, clockAt(21, 0)).length,
    0
  );
  assert.equal(
    context.pendingRevalidationEmails_(revalidationFixture("2026-07-15").index, state, clockAt(10, 0)).length,
    0
  );
});
```

- [ ] **Step 11: Implement revalidation delivery filtering**

Change:

```javascript
function pendingRevalidationEmails_(index, state, clock) {
```

Return no rows when `clock.minutes >= EMAIL_CUTOFF_MINUTES_`, and filter entries
to `entry.status.report_date === clock.date`. Pass `clock` from
`runAutomation`. Keep revalidation workflow dispatch logic unchanged.

- [ ] **Step 12: Run all Apps Script tests**

Run:

```powershell
node --test tests/apps_script_orchestrator.test.mjs
```

Expected: all tests PASS.

- [ ] **Step 13: Commit Task 2**

```powershell
git add apps-script/Code.gs tests/apps_script_orchestrator.test.mjs
git commit -m "feat: gate email on daily opportunities"
```

---

### Task 3: Update operating documentation and regression assertions

**Files:**
- Modify: `README.md`
- Modify: `CLOUD_SETUP.md`
- Modify: `apps-script/README.md`
- Modify: `tests/test_workflow_schedule.py`

**Interfaces:**
- Documents: Apps Script is still the sole sender.
- Documents: 14:00-21:00 normal window, opportunity OR rule, three-state fail-closed behavior, and the conditional 21:00 failure notice.

- [ ] **Step 1: Write failing documentation assertions**

Update `tests/test_workflow_schedule.py` to require these exact concepts in the
three current operating documents:

```python
for text in (readme, cloud_setup, apps_script_readme):
    self.assertIn("14:00-21:00", text)
    self.assertIn("任意一个", text)
    self.assertIn("没有投注方案且没有平局预警时不发送邮件", text)
    self.assertIn("21:00", text)
```

Also assert that current operating docs no longer describe an unconditional
18:00 failure notice:

```python
self.assertNotIn("18:00 仍未就绪时只发一封", combined)
self.assertNotIn("14:00-18:00", combined)
```

Do not scan historical design/specification documents.

- [ ] **Step 2: Run the focused documentation tests and verify RED**

Run:

```powershell
python -m unittest tests.test_workflow_schedule.WorkflowScheduleTest.test_cloud_orchestrator_documentation tests.test_workflow_schedule.WorkflowScheduleTest.test_top_level_docs_replace_the_old_github_email_sender_description -v
```

Expected: FAIL because the docs still state 14:00-18:00 and unconditional 18:00
failure mail.

- [ ] **Step 3: Update current operating documentation**

In all three documents, state:

- Apps Script checks every ten minutes using `Asia/Shanghai`.
- Normal mail may send from 14:00 through 20:59.
- An active simulated plan or a draw alert is sufficient.
- Shadow-only candidates do not qualify.
- Two valid zero counts cause a silent day.
- Unknown opportunity evidence fails closed and stays silent.
- At 21:00, only a proven opportunity with a missed report produces one
  attachment-free failure notice.
- The old GitHub email workflow remains disabled.
- Deployment requires updating the existing Apps Script `Code.gs`; no new
  trigger is needed.

- [ ] **Step 4: Run workflow/documentation tests**

Run:

```powershell
python -m unittest tests.test_workflow_schedule -v
```

Expected: all tests PASS.

- [ ] **Step 5: Commit Task 3**

```powershell
git add README.md CLOUD_SETUP.md apps-script/README.md tests/test_workflow_schedule.py
git commit -m "docs: explain opportunity-gated email window"
```

---

### Task 4: Full verification and PR update

**Files:**
- Verify only: repository working tree and PR branch.

**Interfaces:**
- Confirms Python producer, Apps Script consumer, current documentation, and
  unrelated simulation safeguards remain compatible.

- [ ] **Step 1: Run focused suites together**

```powershell
python -m unittest tests.test_report_status tests.test_workflow_schedule -v
node --test tests/apps_script_orchestrator.test.mjs
```

Expected: all tests PASS with no warnings or unhandled errors.

- [ ] **Step 2: Run the full Python suite**

```powershell
python -m unittest discover -s tests -p "test_*.py"
```

Expected: all tests PASS.

- [ ] **Step 3: Run static repository checks**

```powershell
git diff --check
git status --short
```

Expected: no whitespace errors; only intentional generated or documentation
changes remain.

- [ ] **Step 4: Review the cumulative branch diff**

```powershell
git diff origin/main...HEAD --stat
git log --oneline origin/main..HEAD
```

Expected: the opportunity feature is represented by the design commit and the
three implementation commits, without unrelated file changes.

- [ ] **Step 5: Push the branch and update PR 12**

```powershell
git push origin fix/actions-runtime-regressions
gh pr view 12 --json url,state,headRefName,baseRefName,statusCheckRollup
```

Expected: branch push succeeds, PR 12 remains based on `main`, and new checks
start for the updated head.

- [ ] **Step 6: Verify remote checks**

```powershell
gh pr checks 12 --watch
```

Expected: all required checks PASS. If a check fails, use
`superpowers:systematic-debugging` and `github:gh-fix-ci` before changing code.

- [ ] **Step 7: Record the live deployment handoff**

Confirm the committed `apps-script/Code.gs` still needs to be synchronized to
the existing Google Apps Script project. Do not claim the live sender changed
until that source is updated and a `TEST_MODE=true` run verifies one present and
one absent opportunity fixture.
