import assert from "node:assert/strict";
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const CODE_PATH = path.join(ROOT, "apps-script", "Code.gs");
const MANIFEST_PATH = path.join(ROOT, "apps-script", "appsscript.json");
const CODE = fs.readFileSync(CODE_PATH, "utf8");
const REPORT_DATE = "2026-07-16";
const IMAGE_BYTES = [...Buffer.from("verified-report-image")];
const IMAGE_HASH = crypto.createHash("sha256").update(Buffer.from(IMAGE_BYTES)).digest("hex");
const REVALIDATION_IMAGE_BYTES = [...Buffer.from("verified-revalidation-image")];
const REVALIDATION_IMAGE_HASH = crypto.createHash("sha256").update(Buffer.from(REVALIDATION_IMAGE_BYTES)).digest("hex");

function canonicalJsonBytes(value) {
  const canonical = (item) => {
    if (Array.isArray(item)) return item.map(canonical);
    if (item && typeof item === "object") {
      return Object.fromEntries(Object.keys(item).sort().map((key) => [key, canonical(item[key])]));
    }
    return item;
  };
  return [...Buffer.from(`${JSON.stringify(canonical(value))}\n`, "utf8")];
}

function revalidationCandidate(candidateId, state = "confirmed", overrides = {}) {
  return {
    candidate_id: candidateId,
    state,
    ledger_status: state === "confirmed" ? "ingested" : "not_applicable",
    match: `${candidateId} Home vs Away`,
    market: "win",
    provisional_odds: "2.05",
    current_odds: "2.05",
    provisional_stake: 16,
    final_stake: state === "confirmed" ? 16 : 0,
    current_ev: 0.087,
    reason: state === "confirmed" ? "terms unchanged; final confirmation" : "value threshold failed",
    ...overrides,
  };
}

function revalidationStatus(reportDate, candidates = [revalidationCandidate("c1")], overrides = {}) {
  const changeDigest = crypto.createHash("sha256")
    .update(Buffer.from(canonicalJsonBytes(candidates).slice(0, -1)))
    .digest("hex");
  return {
    schema_version: 1,
    report_date: reportDate,
    revision: 1,
    changed_at_bjt: `${reportDate}T23:55:00+08:00`,
    change_digest: changeDigest,
    changed_candidates: candidates,
    published_candidate_ids: candidates.map((candidate) => candidate.candidate_id).sort(),
    next_revalidation_at_bjt: "",
    all_candidates_terminal: true,
    report_image_url: `web/revalidation/${reportDate}/revision-1-${changeDigest.slice(0, 12)}.png`,
    report_image_sha256: REVALIDATION_IMAGE_HASH,
    source_commit_sha: "0123456789abcdef",
    ...overrides,
  };
}

function revalidationIndex(records = [], generatedAt = "2026-07-20T00:10:00+08:00") {
  return {
    schema_version: 1,
    generated_at_bjt: generatedAt,
    dates: records.map(({ status, statusBytes = canonicalJsonBytes(status) }) => ({
      report_date: status.report_date,
      status_url: `web/revalidation/${status.report_date}/status.json`,
      status_sha256: crypto.createHash("sha256").update(Buffer.from(statusBytes)).digest("hex"),
      revision: status.revision,
      next_revalidation_at_bjt: status.next_revalidation_at_bjt,
    })),
  };
}

function readyStatus(overrides = {}) {
  const quality = {
    source_ready: true,
    fixtures_ready: true,
    zero_fixture_verified: false,
    import_manifest_ready: true,
    odds_ready: true,
    official_odds_complete: true,
    predictions_ready: true,
    decision_bundle_ready: true,
    provisional_plan_ready: true,
    provisional_shadow_ready: true,
    provisional_state_ready: true,
    ledger_ready: true,
    site_ready: true,
    image_ready: true,
  };
  const status = {
    schema_version: 2,
    report_date: REPORT_DATE,
    forecast_ready: true,
    decision_snapshot_ready: false,
    plan_ready: false,
    initial_report_ready: true,
    settlement_ready: true,
    revalidation_ready: true,
    settled_through: "2026-07-15",
    decision_odds_at_bjt: "",
    plan_locked_at_bjt: "",
    provisional_plan_sha256: "a".repeat(64),
    provisional_candidate_count: 0,
    report_stage: "settlement",
    generated_at_bjt: "2026-07-16T13:46:00+08:00",
    build_id: "run-42-settlement",
    image_sha256: IMAGE_HASH,
    source_commit_sha: "0123456789abcdef",
    fixture_count: 2,
    official_fixture_count: 2,
    official_odds_count: 2,
    official_odds_coverage_ratio: 1,
    data_quality: quality,
  };
  return {
    ...status,
    ...overrides,
    data_quality: Object.prototype.hasOwnProperty.call(overrides, "data_quality") ? overrides.data_quality : quality,
  };
}

function schema2ReadyStatus(overrides = {}) {
  return readyStatus(overrides);
}

function zeroFixtureReadyStatus(overrides = {}) {
  return readyStatus({
    fixture_count: 0,
    official_fixture_count: 0,
    official_odds_count: 0,
    official_odds_coverage_ratio: 1,
    decision_odds_at_bjt: "",
    source_status: {
      source: "竞彩网",
      target_date: REPORT_DATE,
      fixture_count: 0,
      no_fixtures: true,
    },
    data_quality: {
      ...readyStatus().data_quality,
      fixtures_ready: true,
      zero_fixture_verified: true,
    },
    ...overrides,
  });
}

function dispatchStatus(overrides = {}) {
  return {
    schema_version: 2,
    report_date: REPORT_DATE,
    forecast_ready: false,
    initial_report_ready: false,
    decision_snapshot_ready: false,
    plan_ready: false,
    settlement_ready: false,
    ...overrides,
  };
}

function response({ code = 200, json, bytes, text } = {}) {
  const body = text ?? (json === undefined ? "" : JSON.stringify(json));
  const bodyBytes = bytes === undefined ? [...Buffer.from(body, "utf8")] : [...bytes];
  return {
    getResponseCode: () => code,
    getContentText: () => bytes === undefined ? body : Buffer.from(bodyBytes).toString("utf8"),
    getBlob: () => ({
      getBytes: () => [...bodyBytes],
      setName(name) {
        this.name = name;
        return this;
      },
    }),
  };
}

function makeHarness({
  now = "2026-07-16T06:00:00.000Z",
  status = readyStatus(),
  imageBytes = IMAGE_BYTES,
  initialProperties = {},
  gmailError = null,
  lockAvailable = true,
  fetchHandler = null,
  triggers = [],
  revalidationIndexValue = revalidationIndex(),
  revalidationIndexBytes = null,
  revalidationStatuses = {},
  revalidationImages = {},
} = {}) {
  const properties = new Map(Object.entries({
    GITHUB_TOKEN: "unit-test-token",
    GITHUB_OWNER: "owner",
    GITHUB_REPO: "repo",
    REPORT_STATUS_URL: "https://example.test/report-status.json",
    REPORT_IMAGE_URL: "https://example.test/daily-report.png",
    REVALIDATION_INDEX_URL: "https://example.test/revalidation-index.json",
    REPORT_SITE_URL: "https://example.test/",
    RECIPIENT_EMAIL: "recipient@example.test",
    ...initialProperties,
  }));
  const calls = { fetch: [], mail: [], logs: [], lock: [], deleted: [], triggerBuilder: [] };
  const scriptProperties = {
    getProperty: (key) => properties.get(key) ?? null,
    getProperties: () => Object.fromEntries(properties),
    setProperty: (key, value) => {
      properties.set(key, String(value));
      return scriptProperties;
    },
  };
  const fixedNow = new Date(now).valueOf();
  class FakeDate extends Date {
    constructor(value) {
      super(value === undefined ? fixedNow : value);
    }
    static now() {
      return fixedNow;
    }
  }
  const defaultFetch = (url, options = {}) => {
    if (url.startsWith("https://example.test/report-status.json?ts=")) {
      return response({ json: status });
    }
    if (url.startsWith("https://example.test/daily-report.png?build_id=")) {
      return response({ bytes: imageBytes });
    }
    if (url.startsWith("https://example.test/revalidation-index.json?ts=")) {
      return response({ bytes: revalidationIndexBytes ?? canonicalJsonBytes(revalidationIndexValue) });
    }
    if (url.startsWith("https://example.test/revalidation/") && url.includes("/status.json?ts=")) {
      const reportDate = url.match(/revalidation\/(\d{4}-\d{2}-\d{2})\/status\.json/)?.[1];
      const statusValue = revalidationStatuses[reportDate];
      if (!statusValue) return response({ code: 404 });
      const bytes = statusValue.bytes ?? canonicalJsonBytes(statusValue.status ?? statusValue);
      return response({ bytes });
    }
    if (url.startsWith("https://example.test/revalidation/") && url.includes("/revision-")) {
      const imagePath = url.replace("https://example.test/", "").split("?")[0];
      return response({ bytes: revalidationImages[imagePath] ?? REVALIDATION_IMAGE_BYTES });
    }
    if (url.startsWith("https://api.github.com/repos/")) {
      return response({ code: 204 });
    }
    throw new Error(`unexpected URL: ${url}`);
  };
  const handler = fetchHandler ?? defaultFetch;
  const context = vm.createContext({
    Date: FakeDate,
    JSON,
    Math,
    encodeURIComponent,
    console,
    Utilities: {
      DigestAlgorithm: { SHA_256: "SHA_256" },
      computeDigest: (algorithm, bytes) => {
        assert.equal(algorithm, "SHA_256");
        return [...crypto.createHash("sha256").update(Buffer.from(bytes)).digest()];
      },
      newBlob: (bytes, contentType, name) => {
        const blobBytes = typeof bytes === "string" ? [...Buffer.from(bytes, "utf8")] : [...bytes];
        return {
          bytes: blobBytes,
          contentType,
          name,
          getBytes: () => [...blobBytes],
          getDataAsString: () => Buffer.from(blobBytes).toString("utf8"),
          setName(nextName) {
            this.name = nextName;
            return this;
          },
        };
      },
      formatDate: (date, timezone, format) => {
        assert.equal(timezone, "Asia/Shanghai");
        assert.equal(format, "yyyy-MM-dd");
        return new Intl.DateTimeFormat("en-CA", {
          timeZone: timezone,
          year: "numeric",
          month: "2-digit",
          day: "2-digit",
        }).format(date);
      },
    },
    UrlFetchApp: {
      fetch: (url, options = {}) => {
        calls.fetch.push({ url, options });
        return handler(url, options);
      },
    },
    GmailApp: {
      sendEmail: (...args) => {
        calls.mail.push(args);
        if (gmailError) throw gmailError;
      },
    },
    PropertiesService: { getScriptProperties: () => scriptProperties },
    LockService: {
      getScriptLock: () => ({
        tryLock: (milliseconds) => {
          calls.lock.push(["try", milliseconds]);
          return lockAvailable;
        },
        releaseLock: () => calls.lock.push(["release"]),
      }),
    },
    ScriptApp: {
      getProjectTriggers: () => triggers,
      deleteTrigger: (trigger) => calls.deleted.push(trigger),
      newTrigger: (handlerName) => {
        calls.triggerBuilder.push(["newTrigger", handlerName]);
        return {
          timeBased() {
            calls.triggerBuilder.push(["timeBased"]);
            return this;
          },
          everyMinutes(minutes) {
            calls.triggerBuilder.push(["everyMinutes", minutes]);
            return this;
          },
          create() {
            calls.triggerBuilder.push(["create"]);
            return this;
          },
        };
      },
    },
    Logger: { log: (message) => calls.logs.push(String(message)) },
  });
  vm.runInContext(CODE, context, { filename: CODE_PATH });
  return { context, calls, properties };
}

function clockAt(hour, minute) {
  return { date: REPORT_DATE, hour, minute, minutes: hour * 60 + minute, nowMs: Date.UTC(2026, 6, 16, hour - 8, minute) };
}

function revalidationFixture(reportDate, {
  candidates = [revalidationCandidate("c1")],
  statusOverrides = {},
  statusBytes = null,
} = {}) {
  const status = revalidationStatus(reportDate, candidates, statusOverrides);
  const exactStatusBytes = statusBytes ?? canonicalJsonBytes(status);
  return {
    status,
    index: revalidationIndex([{ status, statusBytes: exactStatusBytes }]),
    statuses: { [reportDate]: { status, bytes: exactStatusBytes } },
  };
}

test("12:14 does not dispatch", () => {
  const { context } = makeHarness();
  assert.equal(context.chooseDispatch_(clockAt(12, 14), dispatchStatus(), {}), null);
});

test("12:15 dispatches only forecast when forecast is missing", () => {
  const { context } = makeHarness();
  assert.equal(context.chooseDispatch_(clockAt(12, 15), dispatchStatus(), {}), "daily-forecast.yml");
});

test("13:30 waits for forecast before refresh", () => {
  const { context } = makeHarness();
  assert.equal(context.chooseDispatch_(clockAt(13, 30), dispatchStatus(), {}), "daily-forecast.yml");
  assert.equal(context.chooseDispatch_(clockAt(13, 30), dispatchStatus({ forecast_ready: true }), {}), "draw-alert-refresh.yml");
});

test("13:45 waits for provisional report before settlement", () => {
  const { context } = makeHarness();
  const forecastOnly = dispatchStatus({ forecast_ready: true });
  assert.equal(context.chooseDispatch_(clockAt(13, 45), forecastOnly, {}), "draw-alert-refresh.yml");
  assert.equal(context.chooseDispatch_(clockAt(13, 45), { ...forecastOnly, initial_report_ready: true, settlement_ready: false }, {}), "noon-settlement.yml");
});

test("schema 2 advances from provisional readiness to settlement", () => {
  const { context } = makeHarness();
  const status = {
    ...dispatchStatus(),
    schema_version: 2,
    forecast_ready: true,
    initial_report_ready: true,
    decision_snapshot_ready: false,
    plan_ready: false,
    settlement_ready: false,
  };

  assert.equal(context.phaseReady_(status, "refresh"), true);
  assert.equal(context.chooseDispatch_(clockAt(13, 45), status, {}), "noon-settlement.yml");
});

test("legacy schema 1 cannot skip schema 2 dispatch phases", () => {
  const { context } = makeHarness();
  const legacy = {
    ...dispatchStatus(),
    schema_version: 1,
    forecast_ready: true,
    initial_report_ready: true,
    decision_snapshot_ready: true,
    plan_ready: true,
    settlement_ready: false,
  };

  assert.equal(context.chooseDispatch_(clockAt(13, 45), legacy, {}), "daily-forecast.yml");
});

test("same phase dispatches respect the 30-minute cooldown", () => {
  const { context } = makeHarness();
  const state = {
    LAST_FORECAST_DISPATCH_DATE: REPORT_DATE,
    LAST_FORECAST_DISPATCH_AT: String(clockAt(12, 15).nowMs),
  };
  assert.equal(context.chooseDispatch_(clockAt(12, 44), dispatchStatus(), state), null);
  assert.equal(context.chooseDispatch_(clockAt(12, 45), dispatchStatus(), state), "daily-forecast.yml");
});

test("stale readiness flags cannot advance today's dispatch phase", () => {
  const { context } = makeHarness();
  const stale = readyStatus({ report_date: "2026-07-15", settlement_ready: false });
  assert.equal(context.chooseDispatch_(clockAt(13, 45), stale, {}), "daily-forecast.yml");
  assert.equal(context.chooseDispatch_(clockAt(13, 45), readyStatus({ report_date: "", settlement_ready: false }), {}), "daily-forecast.yml");
  assert.equal(context.chooseDispatch_(clockAt(13, 45), readyStatus({ schema_version: 0, settlement_ready: false }), {}), "daily-forecast.yml");
});

test("partial true flags without exact status identity cannot skip dispatch phases", () => {
  const { context } = makeHarness();
  const laterFlags = {
    forecast_ready: true,
    initial_report_ready: true,
    settlement_ready: false,
  };
  const invalidStatuses = [
    laterFlags,
    { ...laterFlags, schema_version: 2 },
    { ...laterFlags, report_date: REPORT_DATE },
    { ...laterFlags, schema_version: 2, report_date: "" },
    { ...laterFlags, schema_version: 2, report_date: "not-a-date" },
    { ...laterFlags, schema_version: "", report_date: REPORT_DATE },
    { ...laterFlags, schema_version: 1, report_date: REPORT_DATE },
    null,
    [],
    "malformed",
  ];
  for (const status of invalidStatuses) {
    assert.equal(context.chooseDispatch_(clockAt(13, 45), status, {}), "daily-forecast.yml");
  }
});

test("beijingClock_ derives Beijing date and wall clock", () => {
  const { context } = makeHarness();
  const clock = context.beijingClock_(new Date("2026-07-16T04:15:00.000Z"));
  assert.deepEqual({ date: clock.date, hour: clock.hour, minute: clock.minute, minutes: clock.minutes }, { date: REPORT_DATE, hour: 12, minute: 15, minutes: 735 });
});

test("reportReadiness_ accepts only the complete current report with exact hash", () => {
  const { context } = makeHarness();
  assert.equal(context.reportReadiness_(readyStatus(), REPORT_DATE, IMAGE_HASH).ready, true);
  assert.equal(context.reportReadiness_(readyStatus(), REPORT_DATE, "0".repeat(64)).ready, false);
});

test("schema 2 report readiness accepts the provisional and settlement contract", () => {
  const { context } = makeHarness();
  const status = schema2ReadyStatus();

  assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, true);
  assert.equal(context.reportReadiness_(status, REPORT_DATE, "0".repeat(64)).ready, false);
});

test("schema 2 report readiness requires initial evidence and revalidation publication", () => {
  const { context } = makeHarness();
  const cases = [
    schema2ReadyStatus({ initial_report_ready: false }),
    schema2ReadyStatus({ revalidation_ready: false }),
    schema2ReadyStatus({ provisional_plan_sha256: "" }),
    schema2ReadyStatus({ report_stage: "provisional" }),
  ];

  for (const status of cases) {
    assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, false);
  }
});

test("schema 2 report readiness ignores obsolete plan lock flags and timestamps", () => {
  const { context } = makeHarness();
  const status = schema2ReadyStatus({
    decision_snapshot_ready: false,
    plan_ready: false,
    decision_odds_at_bjt: "",
    plan_locked_at_bjt: "",
  });

  assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, true);
});

test("reportReadiness_ accepts a strictly proven zero-fixture report", () => {
  const { context } = makeHarness();
  assert.equal(context.reportReadiness_(zeroFixtureReadyStatus(), REPORT_DATE, IMAGE_HASH).ready, true);
});

test("reportReadiness_ rejects forged or incomplete zero-fixture proof", () => {
  const { context } = makeHarness();
  const invalidStatuses = [
    zeroFixtureReadyStatus({ fixture_count: 1 }),
    zeroFixtureReadyStatus({ source_status: { source: "竞彩网", target_date: REPORT_DATE, fixture_count: 0 } }),
    zeroFixtureReadyStatus({ source_status: { source: "竞彩网", target_date: "2026-07-15", fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ source_status: { source: "ESPN", target_date: REPORT_DATE, fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ source_status: { source: "test", target_date: REPORT_DATE, fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ official_odds_count: 1 }),
    zeroFixtureReadyStatus({ official_odds_coverage_ratio: 0 }),
    zeroFixtureReadyStatus({ data_quality: { ...zeroFixtureReadyStatus().data_quality, zero_fixture_verified: false } }),
    zeroFixtureReadyStatus({ data_quality: { ...zeroFixtureReadyStatus().data_quality, fixtures_ready: false } }),
  ];
  for (const status of invalidStatuses) {
    assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, false);
  }
});

test("legacy decision timestamps do not affect zero-fixture readiness", () => {
  const { context } = makeHarness();
  const legacyValues = [undefined, null, 0, false, {}, [], "not-a-date"];
  for (const value of legacyValues) {
    const status = zeroFixtureReadyStatus({ decision_odds_at_bjt: value });
    const readiness = context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH);
    assert.equal(readiness.ready, true, String(value));
  }
});

test("reportReadiness_ fails closed for every required contract field", () => {
  const { context } = makeHarness();
  const invalidCases = [
    ["schema", { schema_version: 1 }],
    ["date", { report_date: "2026-07-15" }],
    ["forecast flag", { forecast_ready: false }],
    ["initial flag", { initial_report_ready: false }],
    ["settlement flag", { settlement_ready: false }],
    ["revalidation flag", { revalidation_ready: false }],
    ["report stage", { report_stage: "provisional" }],
    ["settled through", { settled_through: "2026-07-14" }],
    ["generated timestamp missing", { generated_at_bjt: "" }],
    ["generated timestamp invalid", { generated_at_bjt: "not-a-date" }],
    ["provisional hash blank", { provisional_plan_sha256: "" }],
    ["provisional hash malformed", { provisional_plan_sha256: "abc" }],
    ["candidate count missing", { provisional_candidate_count: undefined }],
    ["candidate count negative", { provisional_candidate_count: -1 }],
    ["fixture count missing", { fixture_count: undefined }],
    ["fixture count negative", { fixture_count: -1 }],
    ["official odds count", { official_odds_count: 1 }],
    ["official odds ratio", { official_odds_coverage_ratio: 0.5 }],
    ["build id", { build_id: "  " }],
    ["source commit", { source_commit_sha: "" }],
    ["hash blank", { image_sha256: "" }],
    ["hash malformed", { image_sha256: "abc" }],
  ];
  for (const [name, overrides] of invalidCases) {
    assert.equal(context.reportReadiness_(readyStatus(overrides), REPORT_DATE, IMAGE_HASH).ready, false, name);
  }
  assert.equal(context.reportReadiness_(readyStatus(), REPORT_DATE, "").ready, false, "empty image bytes/hash");
});

test("reportReadiness_ fails closed when current proving artifacts are invalid", () => {
  const { context } = makeHarness();
  const requiredQualityFields = [
    "source_ready",
    "fixtures_ready",
    "import_manifest_ready",
    "odds_ready",
    "official_odds_complete",
    "predictions_ready",
    "decision_bundle_ready",
    "provisional_plan_ready",
    "provisional_shadow_ready",
    "provisional_state_ready",
    "ledger_ready",
    "site_ready",
    "image_ready",
  ];

  assert.equal(context.reportReadiness_(readyStatus({ data_quality: undefined }), REPORT_DATE, IMAGE_HASH).ready, false);
  for (const field of requiredQualityFields) {
    const quality = { ...readyStatus().data_quality, [field]: false };
    const readiness = context.reportReadiness_(readyStatus({ data_quality: quality }), REPORT_DATE, IMAGE_HASH);
    assert.equal(readiness.ready, false, field);
    assert.ok(readiness.reasons.includes(`data quality invalid: ${field}`), field);
  }
});

test("reportReadiness_ rejects impossible report generation timestamps", () => {
  const { context } = makeHarness();
  const invalidTimestamps = [
    "2026-02-30T13:30:00+08:00",
    "2026-07-16T24:00:00+08:00",
    "2026-07-16T13:60:00+08:00",
    "2026-07-16T13:30:60+08:00",
    "2026-07-16T13:30:00+24:00",
    "2026-07-16T13:30:00+08:60",
    "2026-07-16T13:30:00.1234567+08:00",
    "2026-07-16T13:30:00",
    "",
  ];
  for (const timestamp of invalidTimestamps) {
    const status = readyStatus({ generated_at_bjt: timestamp });
    assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, false, timestamp || "blank");
  }
});

test("reportReadiness_ ignores obsolete decision and plan-lock timestamps", () => {
  const { context } = makeHarness();
  const status = readyStatus({
    decision_odds_at_bjt: "not-a-date",
    plan_locked_at_bjt: { legacy: true },
  });

  assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, true);
});

test("reportReadiness_ accepts valid offsets and fractional report generation times", () => {
  const { context } = makeHarness();
  const status = readyStatus({
    generated_at_bjt: "2026-07-16T13:31:00.123456+08:00",
  });
  assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, true);
  assert.equal(context.reportReadiness_(readyStatus({
    generated_at_bjt: "2026-07-16T11:01:00.5+05:30",
  }), REPORT_DATE, IMAGE_HASH).ready, true);
});

test("missingReasons_ identifies incomplete phases and malformed status", () => {
  const { context } = makeHarness();
  const reasons = context.missingReasons_({ schema_version: 2, report_date: REPORT_DATE, forecast_ready: true }, REPORT_DATE);
  assert.ok(reasons.includes("initial report not ready"));
  assert.ok(reasons.includes("settlement not ready"));
  assert.ok(reasons.includes("revalidation status not ready"));
  assert.ok(context.missingReasons_(null, REPORT_DATE).includes("status unavailable"));
});

test("sha256Hex_ computes lowercase exact SHA-256", () => {
  const { context } = makeHarness();
  assert.equal(context.sha256Hex_(IMAGE_BYTES), IMAGE_HASH);
});

test("00:10 dispatches the due previous Beijing business date with aware inputs", () => {
  const reportDate = "2026-07-19";
  const fixture = revalidationFixture(reportDate, {
    statusOverrides: { next_revalidation_at_bjt: "2026-07-20T00:05:00+08:00", all_candidates_terminal: false },
  });
  const { context, calls, properties } = makeHarness({
    now: "2026-07-19T16:10:00.000Z",
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  const dispatches = calls.fetch.filter((call) => call.url.includes("api.github.com"));
  assert.equal(dispatches.length, 1);
  assert.match(dispatches[0].url, /\/pre-kickoff-revalidation\.yml\/dispatches$/);
  assert.deepEqual(JSON.parse(dispatches[0].options.payload), {
    ref: "main",
    inputs: { target_date: reportDate, now_bjt: "2026-07-20T00:10:00+08:00" },
  });
  assert.equal(properties.get("LAST_REVALIDATION_DISPATCH_DATE"), reportDate);
  assert.equal(properties.has("LAST_REVALIDATION_DISPATCH_ATTEMPT_DATE"), false);
});

test("due revalidation has priority and runAutomation dispatches at most one workflow", () => {
  const fixture = revalidationFixture(REPORT_DATE, {
    statusOverrides: { next_revalidation_at_bjt: `${REPORT_DATE}T13:40:00+08:00`, all_candidates_terminal: false },
  });
  const { context, calls } = makeHarness({
    now: "2026-07-16T05:45:00.000Z",
    status: dispatchStatus(),
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  const dispatches = calls.fetch.filter((call) => call.url.includes("api.github.com"));
  assert.equal(dispatches.length, 1);
  assert.match(dispatches[0].url, /\/pre-kickoff-revalidation\.yml\/dispatches$/);
});

test("revalidation confirmed and ambiguous-attempt cooldown keys are independent", () => {
  const due = {
    dates: [{ report_date: REPORT_DATE, next_revalidation_at_bjt: `${REPORT_DATE}T12:00:00+08:00` }],
  };
  const { context } = makeHarness();
  const clock = clockAt(12, 15);
  assert.equal(context.chooseRevalidationDispatch_(clock, due, {
    LAST_REVALIDATION_DISPATCH_DATE: REPORT_DATE,
    LAST_REVALIDATION_DISPATCH_AT: String(clock.nowMs),
  }), null);
  assert.equal(context.chooseRevalidationDispatch_(clock, due, {
    LAST_REVALIDATION_DISPATCH_ATTEMPT_DATE: REPORT_DATE,
    LAST_REVALIDATION_DISPATCH_ATTEMPT_AT: String(clock.nowMs),
  }), null);
  assert.equal(context.chooseRevalidationDispatch_(clock, due, {
    LAST_FORECAST_DISPATCH_DATE: REPORT_DATE,
    LAST_FORECAST_DISPATCH_AT: String(clock.nowMs),
  }).report_date, REPORT_DATE);
});

test("revalidation index requires canonical exact schema and at most two dates", () => {
  const status = revalidationStatus(REPORT_DATE, [], {
    revision: 0,
    change_digest: "",
    changed_candidates: [],
    published_candidate_ids: [],
    report_image_url: "",
    report_image_sha256: "",
    all_candidates_terminal: false,
  });
  const threeDates = revalidationIndex([
    { status: { ...status, report_date: "2026-07-14", changed_at_bjt: "2026-07-14T12:00:00+08:00" } },
    { status: { ...status, report_date: "2026-07-15", changed_at_bjt: "2026-07-15T12:00:00+08:00" } },
    { status },
  ]);
  const overLimit = makeHarness({ now: "2026-07-16T00:10:00.000Z", revalidationIndexValue: threeDates });
  overLimit.context.runAutomation();
  assert.equal(overLimit.calls.fetch.some((call) => call.url.includes("/revalidation/2026-07-14/status.json")), false);
  assert.ok(overLimit.calls.logs.includes("revalidation index unavailable"));

  const noncanonical = makeHarness({
    now: "2026-07-16T00:10:00.000Z",
    revalidationIndexBytes: [...Buffer.from(JSON.stringify(revalidationIndex()), "utf8")],
  });
  noncanonical.context.runAutomation();
  assert.ok(noncanonical.calls.logs.includes("revalidation index unavailable"));
});

test("status bytes must match the index SHA-256 before dispatch or email", () => {
  const reportDate = "2026-07-19";
  const good = revalidationStatus(reportDate, [revalidationCandidate("c1")], {
    next_revalidation_at_bjt: "2026-07-20T00:05:00+08:00",
    all_candidates_terminal: false,
  });
  const goodBytes = canonicalJsonBytes(good);
  const tampered = { ...good, source_commit_sha: "tampered" };
  const index = revalidationIndex([{ status: good, statusBytes: goodBytes }]);
  const { context, calls } = makeHarness({
    now: "2026-07-19T16:10:00.000Z",
    initialProperties: { LAST_INITIAL_SENT_DATE: reportDate },
    revalidationIndexValue: index,
    revalidationStatuses: { [reportDate]: { bytes: canonicalJsonBytes(tampered) } },
  });

  context.runAutomation();

  assert.equal(calls.fetch.some((call) => call.url.includes("api.github.com")), false);
  assert.equal(calls.mail.length, 0);
});

test("revision image bytes must match status before an update is sent", () => {
  const fixture = revalidationFixture(REPORT_DATE);
  const { context, calls, properties } = makeHarness({
    initialProperties: { LAST_INITIAL_SENT_DATE: REPORT_DATE },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
    revalidationImages: {
      [fixture.status.report_image_url.replace(/^web\//, "")]: [...Buffer.from("tampered")],
    },
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 0);
  assert.equal(properties.has("SENT_REVALIDATION_DIGESTS"), false);
});

test("revalidation update requires a recorded initial email for the same report date", () => {
  const fixture = revalidationFixture(REPORT_DATE);
  const { context, calls, properties } = makeHarness({
    now: "2026-07-15T16:10:00.000Z",
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 0);
  assert.equal(properties.has("SENT_REVALIDATION_DIGESTS"), false);
});

test("one grouped update includes all terminal candidates and unchanged confirmed terms", () => {
  const candidates = [
    revalidationCandidate("c1", "confirmed"),
    revalidationCandidate("c2", "cancelled"),
  ];
  const fixture = revalidationFixture(REPORT_DATE, { candidates });
  const { context, calls, properties } = makeHarness({
    initialProperties: { LAST_INITIAL_SENT_DATE: REPORT_DATE },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][1], `[\u4e34\u573a\u786e\u8ba4] ${REPORT_DATE} \u535a\u5f08\u9884\u6d4b\u65b9\u6848\u66f4\u65b0`);
  assert.match(calls.mail[0][2], /c1/);
  assert.match(calls.mail[0][2], /c2/);
  assert.equal(calls.mail[0][3].attachments.length, 1);
  const sent = JSON.parse(properties.get("SENT_REVALIDATION_DIGESTS"));
  assert.deepEqual(sent[0].candidate_ids, ["c1", "c2"]);
});

test("report_date plus change_digest is sent exactly once", () => {
  const fixture = revalidationFixture(REPORT_DATE);
  const { context, calls, properties } = makeHarness({
    initialProperties: { LAST_INITIAL_SENT_DATE: REPORT_DATE },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();
  context.runAutomation();

  assert.equal(calls.mail.length, 1);
  const sent = JSON.parse(properties.get("SENT_REVALIDATION_DIGESTS"));
  assert.equal(sent.length, 1);
  assert.equal(sent[0].report_date, REPORT_DATE);
  assert.equal(sent[0].change_digest, fixture.status.change_digest);
});

test("failed revalidation Gmail call preserves sent digest state byte-for-byte", () => {
  const fixture = revalidationFixture(REPORT_DATE);
  const oldState = JSON.stringify([{
    report_date: "2026-07-15",
    change_digest: "a".repeat(64),
    sent_at_bjt: "2026-07-15T14:00:00+08:00",
    candidate_ids: ["old"],
  }]);
  const { context, properties } = makeHarness({
    initialProperties: { LAST_INITIAL_SENT_DATE: REPORT_DATE, SENT_REVALIDATION_DIGESTS: oldState },
    gmailError: new Error("gmail unavailable"),
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  assert.throws(() => context.runAutomation(), /gmail unavailable/);
  assert.equal(properties.get("SENT_REVALIDATION_DIGESTS"), oldState);
});

test("sent revalidation digests prune to the latest 30 business dates", () => {
  const fixture = revalidationFixture("2026-07-20");
  const prior = Array.from({ length: 31 }, (_, index) => {
    const reportDate = new Date(Date.UTC(2026, 5, 1 + index)).toISOString().slice(0, 10);
    return {
      report_date: reportDate,
      change_digest: index.toString(16).padStart(64, "0"),
      sent_at_bjt: `${reportDate}T14:00:00+08:00`,
      candidate_ids: [`old-${index}`],
    };
  });
  const { context, properties } = makeHarness({
    now: "2026-07-20T06:00:00.000Z",
    initialProperties: {
      LAST_INITIAL_SENT_DATE: "2026-07-20",
      SENT_REVALIDATION_DIGESTS: JSON.stringify(prior),
    },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  const sent = JSON.parse(properties.get("SENT_REVALIDATION_DIGESTS"));
  assert.equal(new Set(sent.map((entry) => entry.report_date)).size, 30);
  assert.ok(sent.some((entry) => entry.report_date === "2026-07-20"));
  assert.equal(sent.some((entry) => entry.report_date === "2026-06-01"), false);
  assert.equal(sent.some((entry) => entry.report_date === "2026-06-02"), false);
});

test("today's initial failure cutoff does not block a previous-date update", () => {
  const reportDate = "2026-07-19";
  const fixture = revalidationFixture(reportDate);
  const { context, calls } = makeHarness({
    now: "2026-07-20T10:10:00.000Z",
    initialProperties: {
      LAST_INITIAL_SENT_DATE: reportDate,
      LAST_FAILURE_NOTICE_DATE: "2026-07-20",
    },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][1], `[\u4e34\u573a\u786e\u8ba4] ${reportDate} \u535a\u5f08\u9884\u6d4b\u65b9\u6848\u66f4\u65b0`);
});

test("TEST_MODE revalidation dry run does not call Gmail or write sent digests", () => {
  const fixture = revalidationFixture(REPORT_DATE);
  const { context, calls, properties } = makeHarness({
    initialProperties: { LAST_INITIAL_SENT_DATE: REPORT_DATE, TEST_MODE: "true" },
    revalidationIndexValue: fixture.index,
    revalidationStatuses: fixture.statuses,
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 0);
  assert.equal(properties.has("SENT_REVALIDATION_DIGESTS"), false);
  assert.ok(calls.logs.some((entry) => entry.includes("TEST_MODE revalidation update")));
});

test("runAutomation dispatches at most one workflow with required GitHub request", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T04:15:00.000Z", status: { schema_version: 1, report_date: REPORT_DATE, forecast_ready: false } });
  context.runAutomation();
  const dispatches = calls.fetch.filter((call) => call.url.includes("api.github.com"));
  assert.equal(dispatches.length, 1);
  assert.match(dispatches[0].url, /\/daily-forecast\.yml\/dispatches$/);
  assert.equal(dispatches[0].options.method, "post");
  assert.deepEqual(JSON.parse(dispatches[0].options.payload), { ref: "main", inputs: { target_date: REPORT_DATE } });
  assert.deepEqual({ ...dispatches[0].options.headers }, {
    Authorization: "Bearer unit-test-token",
    Accept: "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
  });
  assert.equal(properties.get("LAST_FORECAST_DISPATCH_DATE"), REPORT_DATE);
  assert.equal(calls.lock.at(-1)[0], "release");
});

test("TEST_MODE preserves GitHub dispatch and cooldown state", () => {
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T04:15:00.000Z",
    status: dispatchStatus(),
    initialProperties: { TEST_MODE: "true" },
  });
  context.runAutomation();
  context.runAutomation();
  const dispatches = calls.fetch.filter((call) => call.url.includes("api.github.com"));
  assert.equal(dispatches.length, 1);
  assert.equal(properties.get("LAST_FORECAST_DISPATCH_DATE"), REPORT_DATE);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);
});

test("GitHub dispatch accepts only HTTP 204 and retries known HTTP failures without cooldown state", () => {
  let dispatchAttempts = 0;
  const { context, properties } = makeHarness({
    now: "2026-07-16T04:15:00.000Z",
    status: { schema_version: 1, report_date: REPORT_DATE, forecast_ready: false },
    fetchHandler: (url) => {
      if (url.includes("api.github.com")) {
        dispatchAttempts += 1;
        return response({ code: 200 });
      }
      return response({ json: { schema_version: 1, report_date: REPORT_DATE, forecast_ready: false } });
    },
  });
  assert.throws(() => context.runAutomation(), /dispatch failed/i);
  assert.throws(() => context.runAutomation(), /dispatch failed/i);
  assert.equal(dispatchAttempts, 2);
  assert.equal(properties.has("LAST_FORECAST_DISPATCH_DATE"), false);
  assert.equal(properties.has("LAST_FORECAST_DISPATCH_ATTEMPT_DATE"), false);
});

test("an ambiguous dispatch timeout starts cooldown without recording confirmed success", () => {
  let dispatchAttempts = 0;
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T04:15:00.000Z",
    status: dispatchStatus(),
    fetchHandler: (url) => {
      if (url.includes("api.github.com")) {
        dispatchAttempts += 1;
        throw new Error("timed out after GitHub accepted the dispatch");
      }
      return response({ json: dispatchStatus() });
    },
  });

  assert.throws(() => context.runAutomation(), /timed out after GitHub accepted/);
  context.runAutomation();

  assert.equal(dispatchAttempts, 1);
  assert.equal(calls.fetch.filter((call) => call.url.includes("api.github.com")).length, 1);
  assert.equal(properties.has("LAST_FORECAST_DISPATCH_DATE"), false);
  assert.equal(properties.get("LAST_FORECAST_DISPATCH_ATTEMPT_DATE"), REPORT_DATE);
  assert.equal(properties.get("LAST_FORECAST_DISPATCH_ATTEMPT_AT"), String(clockAt(12, 15).nowMs));
});

test("14:00 rejects yesterday's status without downloading or emailing", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T06:00:00.000Z", status: readyStatus({ report_date: "2026-07-15" }) });
  context.runAutomation();
  assert.equal(calls.fetch.some((call) => call.url.includes("daily-report.png")), false);
  assert.equal(calls.mail.length, 0);
});

test("normal email cannot send before 14:00", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T05:59:00.000Z" });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
  assert.equal(calls.fetch.some((call) => call.url.includes("daily-report.png")), false);
});

test("initial email waits when the revalidation index is unavailable", () => {
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T06:00:00.000Z",
    fetchHandler: (url) => {
      if (url.startsWith("https://example.test/revalidation-index.json?ts=")) return response({ code: 503 });
      if (url.startsWith("https://example.test/report-status.json?ts=")) return response({ json: readyStatus() });
      if (url.startsWith("https://example.test/daily-report.png?build_id=")) return response({ bytes: IMAGE_BYTES });
      throw new Error(`unexpected URL: ${url}`);
    },
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 0);
  assert.equal(calls.fetch.some((call) => call.url.includes("daily-report.png")), false);
  assert.equal(properties.has("LAST_INITIAL_SENT_DATE"), false);
});

test("initial email requires today's revalidation entry when provisional candidates exist", () => {
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T06:00:00.000Z",
    status: readyStatus({ provisional_candidate_count: 1 }),
    revalidationIndexValue: revalidationIndex(),
  });

  context.runAutomation();

  assert.equal(calls.mail.length, 0);
  assert.equal(calls.fetch.some((call) => call.url.includes("daily-report.png")), false);
  assert.equal(properties.has("LAST_INITIAL_SENT_DATE"), false);
});

test("revalidation coverage accepts zero candidates or today's entry, never a prior-only index", () => {
  const { context } = makeHarness();
  const current = revalidationFixture(REPORT_DATE).index;
  const priorOnly = revalidationFixture("2026-07-15").index;

  assert.equal(context.revalidationIndexCoversReport_(revalidationIndex(), readyStatus()), true);
  assert.equal(context.revalidationIndexCoversReport_(current, readyStatus({ provisional_candidate_count: 1 })), true);
  assert.equal(context.revalidationIndexCoversReport_(priorOnly, readyStatus({ provisional_candidate_count: 1 })), false);
});

test("ready status plus matching image hash sends once and persists after Gmail", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T06:00:00.000Z" });
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][0], "recipient@example.test");
  assert.equal(calls.mail[0][3].attachments.length, 1);
  assert.equal(properties.get("LAST_INITIAL_SENT_DATE"), REPORT_DATE);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.get("LAST_SENT_IMAGE_SHA256"), IMAGE_HASH);
});

test("mismatched image hash never sends", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T06:00:00.000Z", imageBytes: [...Buffer.from("tampered")] });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
});

test("empty image bytes never send", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T06:00:00.000Z", imageBytes: [] });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
});

test("18:00 incomplete state sends one attachment-free failure notice", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T10:00:00.000Z", status: { schema_version: 1, report_date: REPORT_DATE, forecast_ready: false } });
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][3]?.attachments, undefined);
  assert.equal(properties.get("LAST_FAILURE_NOTICE_DATE"), REPORT_DATE);
  assert.equal(calls.fetch.some((call) => call.url.includes("api.github.com")), false);
});

test("18:00 failure notice includes report timestamp and dashboard without attachments", () => {
  const generatedAt = "2026-07-16T17:42:00+08:00";
  const { context, calls } = makeHarness({
    now: "2026-07-16T10:00:00.000Z",
    status: dispatchStatus({ generated_at_bjt: generatedAt }),
  });

  context.runAutomation();
  context.runAutomation();

  assert.equal(calls.mail.length, 1);
  assert.match(calls.mail[0][2], new RegExp(generatedAt.replace(/[+]/g, "\\+")));
  assert.match(calls.mail[0][2], /https:\/\/example\.test\//);
  assert.match(calls.mail[0][3].htmlBody, new RegExp(generatedAt.replace(/[+]/g, "\\+")));
  assert.match(calls.mail[0][3].htmlBody, /href="https:\/\/example\.test\/"/);
  assert.equal(calls.mail[0][3].attachments, undefined);
});

test("18:00 gives a currently ready normal report priority", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T10:00:00.000Z" });
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][3].attachments.length, 1);
  assert.equal(properties.get("LAST_INITIAL_SENT_DATE"), REPORT_DATE);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);
  assert.equal(calls.fetch.some((call) => call.url.includes("api.github.com")), false);
});

test("18:00 treats malformed status JSON as incomplete and records the reason", () => {
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T10:00:00.000Z",
    fetchHandler: () => response({ text: "{" }),
  });
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.match(calls.mail[0][2], /status fetch\/parse failed/);
  assert.equal(calls.mail[0][2].match(/status fetch\/parse failed/g)?.length, 1);
  assert.equal(properties.get("LAST_FAILURE_NOTICE_DATE"), REPORT_DATE);
});

test("a report becoming ready after failure notice does not send or dispatch late", () => {
  const { context, calls } = makeHarness({ now: "2026-07-16T10:10:00.000Z", initialProperties: { LAST_FAILURE_NOTICE_DATE: REPORT_DATE } });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
  assert.equal(calls.fetch.filter((call) => !call.url.includes("revalidation-index.json")).length, 0);
});

test("normal and failure mail both deduplicate by Beijing date", () => {
  const sent = makeHarness({ now: "2026-07-16T06:00:00.000Z", initialProperties: { LAST_SENT_DATE: REPORT_DATE } });
  sent.context.runAutomation();
  assert.equal(sent.calls.mail.length, 0);
  const failed = makeHarness({ now: "2026-07-16T10:00:00.000Z", status: { schema_version: 1, report_date: REPORT_DATE }, initialProperties: { LAST_FAILURE_NOTICE_DATE: REPORT_DATE } });
  failed.context.runAutomation();
  assert.equal(failed.calls.mail.length, 0);
});

test("failed Gmail call does not write normal sent state", () => {
  const { context, properties } = makeHarness({ now: "2026-07-16T06:00:00.000Z", gmailError: new Error("gmail unavailable") });
  assert.throws(() => context.runAutomation(), /gmail unavailable/);
  assert.equal(properties.has("LAST_INITIAL_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_IMAGE_SHA256"), false);
});

test("failed failure-notice Gmail call does not write notice state", () => {
  const { context, properties } = makeHarness({ now: "2026-07-16T10:00:00.000Z", status: {}, gmailError: new Error("gmail unavailable") });
  assert.throws(() => context.runAutomation(), /gmail unavailable/);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);
});

test("an unavailable lock exits without work and an acquired lock releases on failure", () => {
  const unavailable = makeHarness({ lockAvailable: false });
  unavailable.context.runAutomation();
  assert.deepEqual(unavailable.calls.lock, [["try", 5000]]);
  assert.equal(unavailable.calls.fetch.length, 0);
  const failing = makeHarness({ gmailError: new Error("boom") });
  assert.throws(() => failing.context.runAutomation(), /boom/);
  assert.equal(failing.calls.lock.at(-1)[0], "release");
});

test("TEST_MODE normal dry run leaves production mail state untouched and permits same-day send", () => {
  const { context, calls, properties } = makeHarness({ initialProperties: { TEST_MODE: "true" } });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
  assert.ok(calls.logs.some((entry) => entry.includes("TEST_MODE normal report")));
  assert.equal(properties.has("LAST_INITIAL_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_IMAGE_SHA256"), false);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);

  properties.set("TEST_MODE", "false");
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(properties.get("LAST_INITIAL_SENT_DATE"), REPORT_DATE);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.get("LAST_SENT_IMAGE_SHA256"), IMAGE_HASH);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);
});

test("TEST_MODE failure dry run leaves production mail state untouched and permits same-day notice", () => {
  const { context, calls, properties } = makeHarness({
    now: "2026-07-16T10:00:00.000Z",
    status: dispatchStatus(),
    initialProperties: { TEST_MODE: "true" },
  });
  context.runAutomation();
  assert.equal(calls.mail.length, 0);
  assert.ok(calls.logs.some((entry) => entry.includes("TEST_MODE failure notice")));
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_IMAGE_SHA256"), false);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);

  properties.set("TEST_MODE", "false");
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][3]?.attachments, undefined);
  assert.equal(properties.get("LAST_FAILURE_NOTICE_DATE"), REPORT_DATE);
  assert.equal(properties.has("LAST_SENT_DATE"), false);
});

test("installAutomationTrigger deletes both legacy handlers and creates one 10-minute trigger", () => {
  const triggers = [
    { getHandlerFunction: () => "runAutomation", id: 1 },
    { getHandlerFunction: () => "sendDailyReport", id: 2 },
    { getHandlerFunction: () => "unrelated", id: 3 },
  ];
  const { context, calls } = makeHarness({ triggers });
  context.installAutomationTrigger();
  assert.deepEqual(calls.deleted.map((trigger) => trigger.id), [1, 2]);
  assert.deepEqual(calls.triggerBuilder, [["newTrigger", "runAutomation"], ["timeBased"], ["everyMinutes", 10], ["create"]]);
});

test("sendDailyReport remains a compatibility wrapper", () => {
  const { context, calls } = makeHarness();
  context.sendDailyReport();
  assert.equal(calls.mail.length, 1);
});

test("manifest exactly declares the required timezone, runtime, and scopes", () => {
  assert.deepEqual(JSON.parse(fs.readFileSync(MANIFEST_PATH, "utf8")), {
    timeZone: "Asia/Shanghai",
    dependencies: {},
    exceptionLogging: "STACKDRIVER",
    runtimeVersion: "V8",
    oauthScopes: [
      "https://www.googleapis.com/auth/script.external_request",
      "https://www.googleapis.com/auth/script.scriptapp",
      "https://www.googleapis.com/auth/gmail.send",
    ],
  });
});
