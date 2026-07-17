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

function readyStatus(overrides = {}) {
  return {
    schema_version: 1,
    report_date: REPORT_DATE,
    forecast_ready: true,
    decision_snapshot_ready: true,
    plan_ready: true,
    settlement_ready: true,
    settled_through: "2026-07-15",
    decision_odds_at_bjt: "2026-07-16T13:30:00+08:00",
    plan_locked_at_bjt: "2026-07-16T13:31:00+08:00",
    generated_at_bjt: "2026-07-16T13:46:00+08:00",
    build_id: "run-42-settlement",
    image_sha256: IMAGE_HASH,
    source_commit_sha: "0123456789abcdef",
    data_quality: {
      predictions_ready: true,
      plan_csv_ready: true,
      plan_lock_ready: true,
      decision_snapshot_ready: true,
      ledger_ready: true,
    },
    ...overrides,
  };
}

function zeroFixtureReadyStatus(overrides = {}) {
  return readyStatus({
    fixture_count: 0,
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
      decision_snapshot_ready: true,
    },
    ...overrides,
  });
}

function dispatchStatus(overrides = {}) {
  return {
    schema_version: 1,
    report_date: REPORT_DATE,
    forecast_ready: false,
    decision_snapshot_ready: false,
    plan_ready: false,
    settlement_ready: false,
    ...overrides,
  };
}

function response({ code = 200, json, bytes = [], text } = {}) {
  const body = text ?? (json === undefined ? "" : JSON.stringify(json));
  return {
    getResponseCode: () => code,
    getContentText: () => body,
    getBlob: () => ({
      getBytes: () => [...bytes],
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
} = {}) {
  const properties = new Map(Object.entries({
    GITHUB_TOKEN: "unit-test-token",
    GITHUB_OWNER: "owner",
    GITHUB_REPO: "repo",
    REPORT_STATUS_URL: "https://example.test/report-status.json",
    REPORT_IMAGE_URL: "https://example.test/daily-report.png",
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
      newBlob: (bytes, contentType, name) => ({
        bytes: [...bytes],
        contentType,
        name,
        getBytes: () => [...bytes],
        setName(nextName) {
          this.name = nextName;
          return this;
        },
      }),
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

test("13:45 waits for decision before settlement", () => {
  const { context } = makeHarness();
  const forecastOnly = dispatchStatus({ forecast_ready: true });
  assert.equal(context.chooseDispatch_(clockAt(13, 45), forecastOnly, {}), "draw-alert-refresh.yml");
  assert.equal(context.chooseDispatch_(clockAt(13, 45), { ...forecastOnly, decision_snapshot_ready: true, plan_ready: true, settlement_ready: false }, {}), "noon-settlement.yml");
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
    decision_snapshot_ready: true,
    plan_ready: true,
    settlement_ready: false,
  };
  const invalidStatuses = [
    laterFlags,
    { ...laterFlags, schema_version: 1 },
    { ...laterFlags, report_date: REPORT_DATE },
    { ...laterFlags, schema_version: 1, report_date: "" },
    { ...laterFlags, schema_version: 1, report_date: "not-a-date" },
    { ...laterFlags, schema_version: "", report_date: REPORT_DATE },
    { ...laterFlags, schema_version: 2, report_date: REPORT_DATE },
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

test("reportReadiness_ accepts a strictly proven zero-fixture report without decision timestamp", () => {
  const { context } = makeHarness();
  assert.equal(context.reportReadiness_(zeroFixtureReadyStatus(), REPORT_DATE, IMAGE_HASH).ready, true);
  assert.equal(context.reportReadiness_(zeroFixtureReadyStatus({
    decision_odds_at_bjt: "2026-07-16T13:30:00+08:00",
  }), REPORT_DATE, IMAGE_HASH).ready, true);
});

test("reportReadiness_ rejects forged or incomplete zero-fixture timestamp exemptions", () => {
  const { context } = makeHarness();
  const invalidStatuses = [
    zeroFixtureReadyStatus({ fixture_count: 1 }),
    zeroFixtureReadyStatus({ source_status: { source: "竞彩网", target_date: REPORT_DATE, fixture_count: 0 } }),
    zeroFixtureReadyStatus({ source_status: { source: "竞彩网", target_date: "2026-07-15", fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ source_status: { source: "ESPN", target_date: REPORT_DATE, fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ source_status: { source: "test", target_date: REPORT_DATE, fixture_count: 0, no_fixtures: true } }),
    zeroFixtureReadyStatus({ data_quality: { ...zeroFixtureReadyStatus().data_quality, zero_fixture_verified: false } }),
    zeroFixtureReadyStatus({ decision_snapshot_ready: false }),
    zeroFixtureReadyStatus({ data_quality: { ...zeroFixtureReadyStatus().data_quality, decision_snapshot_ready: false } }),
  ];
  for (const status of invalidStatuses) {
    assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, false);
  }
});

test("zero-fixture decision timestamp accepts only empty text or valid ISO text", () => {
  const { context } = makeHarness();
  const invalidValues = [undefined, null, 0, false, {}, []];
  for (const value of invalidValues) {
    const status = zeroFixtureReadyStatus({ decision_odds_at_bjt: value });
    const readiness = context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH);
    assert.equal(readiness.ready, false, String(value));
    assert.ok(readiness.reasons.includes("decision timestamp invalid"), String(value));
  }
});

test("reportReadiness_ fails closed for every required contract field", () => {
  const { context } = makeHarness();
  const invalidCases = [
    ["schema", { schema_version: 2 }],
    ["date", { report_date: "2026-07-15" }],
    ["forecast flag", { forecast_ready: false }],
    ["decision flag", { decision_snapshot_ready: false }],
    ["plan flag", { plan_ready: false }],
    ["settlement flag", { settlement_ready: false }],
    ["settled through", { settled_through: "2026-07-14" }],
    ["generated timestamp missing", { generated_at_bjt: "" }],
    ["generated timestamp invalid", { generated_at_bjt: "not-a-date" }],
    ["decision timestamp invalid", { decision_odds_at_bjt: "not-a-date" }],
    ["lock timestamp missing", { plan_locked_at_bjt: "" }],
    ["lock timestamp invalid", { plan_locked_at_bjt: "not-a-date" }],
    ["timestamp order", { plan_locked_at_bjt: "2026-07-16T14:01:00+08:00", generated_at_bjt: "2026-07-16T14:00:00+08:00" }],
    ["build id", { build_id: "  " }],
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
    "predictions_ready",
    "plan_csv_ready",
    "plan_lock_ready",
    "decision_snapshot_ready",
    "ledger_ready",
  ];

  assert.equal(context.reportReadiness_(readyStatus({ data_quality: undefined }), REPORT_DATE, IMAGE_HASH).ready, false);
  for (const field of requiredQualityFields) {
    const quality = { ...readyStatus().data_quality, [field]: false };
    const readiness = context.reportReadiness_(readyStatus({ data_quality: quality }), REPORT_DATE, IMAGE_HASH);
    assert.equal(readiness.ready, false, field);
    assert.ok(readiness.reasons.includes(`data quality invalid: ${field}`), field);
  }
});

test("reportReadiness_ rejects impossible and out-of-range ISO timestamps", () => {
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
    const status = readyStatus({ decision_odds_at_bjt: timestamp });
    assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, false, timestamp || "blank");
  }
});

test("reportReadiness_ enforces decision then lock then generation ordering", () => {
  const { context } = makeHarness();
  const invalidOrders = [
    {
      decision_odds_at_bjt: "2026-07-16T14:01:00+08:00",
      plan_locked_at_bjt: "2026-07-16T13:31:00+08:00",
      generated_at_bjt: "2026-07-16T14:00:00+08:00",
    },
    {
      decision_odds_at_bjt: "2026-07-16T13:31:00+08:00",
      plan_locked_at_bjt: "2026-07-16T13:30:00+08:00",
      generated_at_bjt: "2026-07-16T14:00:00+08:00",
    },
    {
      decision_odds_at_bjt: "2026-07-16T13:30:00+08:00",
      plan_locked_at_bjt: "2026-07-16T14:01:00+08:00",
      generated_at_bjt: "2026-07-16T14:00:00+08:00",
    },
  ];
  for (const timestamps of invalidOrders) {
    assert.equal(context.reportReadiness_(readyStatus(timestamps), REPORT_DATE, IMAGE_HASH).ready, false);
  }
});

test("reportReadiness_ accepts valid offsets, fractional seconds, and equal causal times", () => {
  const { context } = makeHarness();
  const status = readyStatus({
    decision_odds_at_bjt: "2026-07-16T11:00:00.123456+05:30",
    plan_locked_at_bjt: "2026-07-16T05:31:00.123456Z",
    generated_at_bjt: "2026-07-16T13:31:00.123456+08:00",
  });
  assert.equal(context.reportReadiness_(status, REPORT_DATE, IMAGE_HASH).ready, true);
  const equal = "2026-07-16T13:31:00.5+08:00";
  assert.equal(context.reportReadiness_(readyStatus({
    decision_odds_at_bjt: equal,
    plan_locked_at_bjt: equal,
    generated_at_bjt: equal,
  }), REPORT_DATE, IMAGE_HASH).ready, true);
});

test("missingReasons_ identifies incomplete phases and malformed status", () => {
  const { context } = makeHarness();
  const reasons = context.missingReasons_({ schema_version: 1, report_date: REPORT_DATE, forecast_ready: true }, REPORT_DATE);
  assert.ok(reasons.includes("decision not ready"));
  assert.ok(reasons.includes("settlement not ready"));
  assert.ok(context.missingReasons_(null, REPORT_DATE).includes("status unavailable"));
});

test("sha256Hex_ computes lowercase exact SHA-256", () => {
  const { context } = makeHarness();
  assert.equal(context.sha256Hex_(IMAGE_BYTES), IMAGE_HASH);
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

test("ready status plus matching image hash sends once and persists after Gmail", () => {
  const { context, calls, properties } = makeHarness({ now: "2026-07-16T06:00:00.000Z" });
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(calls.mail[0][0], "recipient@example.test");
  assert.equal(calls.mail[0][3].attachments.length, 1);
  assert.equal(properties.get("LAST_SENT_DATE"), REPORT_DATE);
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
  assert.equal(properties.get("LAST_SENT_DATE"), REPORT_DATE);
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
  assert.equal(calls.fetch.length, 0);
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
  assert.equal(properties.has("LAST_SENT_DATE"), false);
  assert.equal(properties.has("LAST_SENT_IMAGE_SHA256"), false);
  assert.equal(properties.has("LAST_FAILURE_NOTICE_DATE"), false);

  properties.set("TEST_MODE", "false");
  context.runAutomation();
  context.runAutomation();
  assert.equal(calls.mail.length, 1);
  assert.equal(properties.get("LAST_SENT_DATE"), REPORT_DATE);
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
