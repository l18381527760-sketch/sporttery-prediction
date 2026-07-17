var BEIJING_OFFSET_MS_ = 8 * 60 * 60 * 1000;
var FORECAST_WORKFLOW_ = "daily-forecast.yml";
var REFRESH_WORKFLOW_ = "draw-alert-refresh.yml";
var SETTLEMENT_WORKFLOW_ = "noon-settlement.yml";
var DISPATCH_COOLDOWN_MS_ = 30 * 60 * 1000;
var OFFICIAL_FIXTURE_SOURCES_ = ["竞彩网", "中国足彩网", "sporttery", "zgzcw"];
var REQUIRED_REPORT_QUALITY_FIELDS_ = [
  "predictions_ready",
  "plan_csv_ready",
  "plan_lock_ready",
  "decision_snapshot_ready",
  "ledger_ready",
];

function pad2_(value) {
  return value < 10 ? "0" + value : String(value);
}

function beijingClock_(now) {
  var instant = now || new Date();
  var shifted = new Date(instant.getTime() + BEIJING_OFFSET_MS_);
  var year = shifted.getUTCFullYear();
  var month = shifted.getUTCMonth() + 1;
  var day = shifted.getUTCDate();
  var hour = shifted.getUTCHours();
  var minute = shifted.getUTCMinutes();
  return {
    date: year + "-" + pad2_(month) + "-" + pad2_(day),
    hour: hour,
    minute: minute,
    minutes: hour * 60 + minute,
    nowMs: instant.getTime(),
  };
}

function previousDate_(dateText) {
  var parts = String(dateText).split("-");
  if (parts.length !== 3) return "";
  var instant = Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2]));
  if (!isFinite(instant)) return "";
  var prior = new Date(instant - 24 * 60 * 60 * 1000);
  return prior.getUTCFullYear() + "-" + pad2_(prior.getUTCMonth() + 1) + "-" + pad2_(prior.getUTCDate());
}

function validDateText_(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(String(value || ""))) return false;
  var parts = value.split("-");
  var parsed = new Date(Date.UTC(Number(parts[0]), Number(parts[1]) - 1, Number(parts[2])));
  return parsed.getUTCFullYear() === Number(parts[0]) &&
    parsed.getUTCMonth() + 1 === Number(parts[1]) &&
    parsed.getUTCDate() === Number(parts[2]);
}

function timestampMillis_(value) {
  if (typeof value !== "string") return NaN;
  var match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{1,6}))?(Z|([+-])(\d{2}):(\d{2}))$/.exec(value);
  if (!match) return NaN;

  var year = Number(match[1]);
  var month = Number(match[2]);
  var day = Number(match[3]);
  var hour = Number(match[4]);
  var minute = Number(match[5]);
  var second = Number(match[6]);
  var offsetHour = match[8] === "Z" ? 0 : Number(match[10]);
  var offsetMinute = match[8] === "Z" ? 0 : Number(match[11]);
  if (year < 1 || month < 1 || month > 12 || hour > 23 || minute > 59 || second > 59 ||
      offsetHour > 23 || offsetMinute > 59) return NaN;

  var leapYear = year % 4 === 0 && (year % 100 !== 0 || year % 400 === 0);
  var monthDays = [31, leapYear ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (day < 1 || day > monthDays[month - 1]) return NaN;

  var local = new Date(0);
  local.setUTCFullYear(year, month - 1, day);
  local.setUTCHours(hour, minute, second, 0);
  if (local.getUTCFullYear() !== year || local.getUTCMonth() + 1 !== month || local.getUTCDate() !== day ||
      local.getUTCHours() !== hour || local.getUTCMinutes() !== minute || local.getUTCSeconds() !== second) return NaN;

  var offsetSign = match[9] === "-" ? -1 : 1;
  var offsetMillis = offsetSign * (offsetHour * 60 + offsetMinute) * 60 * 1000;
  var instantMillis = local.getTime() - offsetMillis;
  var roundTrip = new Date(instantMillis + offsetMillis);
  if (roundTrip.getUTCFullYear() !== year || roundTrip.getUTCMonth() + 1 !== month || roundTrip.getUTCDate() !== day ||
      roundTrip.getUTCHours() !== hour || roundTrip.getUTCMinutes() !== minute || roundTrip.getUTCSeconds() !== second) return NaN;

  var fraction = match[7] || "";
  while (fraction.length < 6) fraction += "0";
  return instantMillis * 1000 + Number(fraction || "0");
}

function verifiedZeroFixtureDay_(status, expectedDate) {
  var source = status && status.source_status;
  var quality = status && status.data_quality;
  return status && status.fixture_count === 0 &&
    source && typeof source === "object" && !Array.isArray(source) &&
    OFFICIAL_FIXTURE_SOURCES_.indexOf(source.source) !== -1 &&
    source.target_date === expectedDate && source.fixture_count === 0 && source.no_fixtures === true &&
    quality && typeof quality === "object" && !Array.isArray(quality) &&
    quality.fixtures_ready === true && quality.zero_fixture_verified === true &&
    status.decision_snapshot_ready === true && quality.decision_snapshot_ready === true;
}

function missingReasons_(status, expectedDate) {
  var reasons = [];
  if (!status || typeof status !== "object" || Array.isArray(status)) {
    return ["status unavailable"];
  }
  if (status.schema_version !== 1) reasons.push("unsupported schema version");
  if (status.report_date !== expectedDate) reasons.push("report date mismatch");
  if (status.forecast_ready !== true) reasons.push("forecast not ready");
  if (status.decision_snapshot_ready !== true || status.plan_ready !== true) reasons.push("decision not ready");
  if (status.settlement_ready !== true) reasons.push("settlement not ready");
  if (!validDateText_(status.settled_through) || status.settled_through < previousDate_(expectedDate)) {
    reasons.push("settlement is not current");
  }

  var generatedAt = timestampMillis_(status.generated_at_bjt);
  var decisionAt = timestampMillis_(status.decision_odds_at_bjt);
  var lockedAt = timestampMillis_(status.plan_locked_at_bjt);
  var zeroFixtureDay = verifiedZeroFixtureDay_(status, expectedDate);
  if (!isFinite(generatedAt)) reasons.push("generated timestamp invalid");
  if (!zeroFixtureDay && !isFinite(decisionAt)) reasons.push("decision timestamp invalid");
  if (zeroFixtureDay && status.decision_odds_at_bjt !== "" && (
      typeof status.decision_odds_at_bjt !== "string" || !isFinite(decisionAt)
  )) reasons.push("decision timestamp invalid");
  if (!isFinite(lockedAt)) reasons.push("plan lock timestamp invalid");
  if (isFinite(decisionAt) && isFinite(lockedAt) && decisionAt > lockedAt) reasons.push("decision timestamp is later than plan lock");
  if (isFinite(decisionAt) && isFinite(generatedAt) && decisionAt > generatedAt) reasons.push("decision timestamp is later than report generation");
  if (isFinite(generatedAt) && isFinite(lockedAt) && lockedAt > generatedAt) reasons.push("plan lock is later than report generation");

  if (typeof status.build_id !== "string" || !status.build_id.trim()) reasons.push("build id missing");
  if (typeof status.source_commit_sha !== "string" || !status.source_commit_sha.trim()) reasons.push("source commit missing");
  if (typeof status.image_sha256 !== "string" || !/^[0-9a-f]{64}$/.test(status.image_sha256)) reasons.push("image hash invalid");
  return reasons;
}

function reportReadiness_(status, expectedDate, imageSha256) {
  var reasons = missingReasons_(status, expectedDate);
  var quality = status && status.data_quality;
  REQUIRED_REPORT_QUALITY_FIELDS_.forEach(function (field) {
    if (!quality || typeof quality !== "object" || Array.isArray(quality) || quality[field] !== true) {
      reasons.push("data quality invalid: " + field);
    }
  });
  if (typeof imageSha256 !== "string" || !/^[0-9a-f]{64}$/.test(imageSha256)) {
    reasons.push("image bytes empty or hash invalid");
  } else if (status && status.image_sha256 !== imageSha256) {
    reasons.push("image hash mismatch");
  }
  return { ready: reasons.length === 0, reasons: reasons };
}

function phaseReady_(status, phase) {
  if (!status || typeof status !== "object") return false;
  if (phase === "forecast") return status.forecast_ready === true;
  if (phase === "refresh") return status.decision_snapshot_ready === true && status.plan_ready === true;
  return status.settlement_ready === true;
}

function cooldownAllows_(clock, state, phase) {
  var prefix = phase === "forecast" ? "FORECAST" : phase === "refresh" ? "REFRESH" : "SETTLEMENT";
  var confirmedDateKey = "LAST_" + prefix + "_DISPATCH_DATE";
  var confirmedAtKey = "LAST_" + prefix + "_DISPATCH_AT";
  var attemptDateKey = "LAST_" + prefix + "_DISPATCH_ATTEMPT_DATE";
  var attemptAtKey = "LAST_" + prefix + "_DISPATCH_ATTEMPT_AT";
  return cooldownElapsed_(clock, state, confirmedDateKey, confirmedAtKey) &&
    cooldownElapsed_(clock, state, attemptDateKey, attemptAtKey);
}

function cooldownElapsed_(clock, state, dateKey, atKey) {
  if (state[dateKey] !== clock.date) return true;
  var prior = Number(state[atKey]);
  return isFinite(prior) && clock.nowMs - prior >= DISPATCH_COOLDOWN_MS_;
}

function chooseDispatch_(clock, status, state) {
  var trustedStatus = status !== null && typeof status === "object" && !Array.isArray(status) &&
    status.schema_version === 1 && status.report_date === clock.date;
  var current = trustedStatus ? status : {};
  var saved = state || {};
  if (clock.minutes >= 18 * 60) return null;
  if (!phaseReady_(current, "forecast")) {
    return clock.minutes >= 12 * 60 + 15 && cooldownAllows_(clock, saved, "forecast") ? FORECAST_WORKFLOW_ : null;
  }
  if (!phaseReady_(current, "refresh")) {
    return clock.minutes >= 13 * 60 + 30 && cooldownAllows_(clock, saved, "refresh") ? REFRESH_WORKFLOW_ : null;
  }
  if (!phaseReady_(current, "settlement")) {
    return clock.minutes >= 13 * 60 + 45 && cooldownAllows_(clock, saved, "settlement") ? SETTLEMENT_WORKFLOW_ : null;
  }
  return null;
}

function sha256Hex_(bytes) {
  var digest = Utilities.computeDigest(Utilities.DigestAlgorithm.SHA_256, bytes);
  return digest.map(function (value) {
    var unsigned = (Number(value) + 256) % 256;
    return (unsigned < 16 ? "0" : "") + unsigned.toString(16);
  }).join("");
}

function requiredProperty_(properties, key) {
  var value = properties.getProperty(key);
  if (typeof value !== "string" || !value.trim()) throw new Error("Missing required script property: " + key);
  return value.trim();
}

function fetchStatus_(properties, clock) {
  try {
    var url = requiredProperty_(properties, "REPORT_STATUS_URL") + "?ts=" + clock.nowMs;
    var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
    if (response.getResponseCode() !== 200) throw new Error("status HTTP " + response.getResponseCode());
    var status = JSON.parse(response.getContentText());
    if (!status || typeof status !== "object" || Array.isArray(status)) throw new Error("status JSON is not an object");
    return { status: status, reasons: [] };
  } catch (error) {
    var reason = "status fetch/parse failed: " + String(error && error.message ? error.message : error);
    Logger.log(reason);
    return { status: null, reasons: [reason] };
  }
}

function dispatchStateKeys_(workflow) {
  if (workflow === FORECAST_WORKFLOW_) return ["LAST_FORECAST_DISPATCH_DATE", "LAST_FORECAST_DISPATCH_AT"];
  if (workflow === REFRESH_WORKFLOW_) return ["LAST_REFRESH_DISPATCH_DATE", "LAST_REFRESH_DISPATCH_AT"];
  return ["LAST_SETTLEMENT_DISPATCH_DATE", "LAST_SETTLEMENT_DISPATCH_AT"];
}

function dispatchAttemptStateKeys_(workflow) {
  if (workflow === FORECAST_WORKFLOW_) return ["LAST_FORECAST_DISPATCH_ATTEMPT_DATE", "LAST_FORECAST_DISPATCH_ATTEMPT_AT"];
  if (workflow === REFRESH_WORKFLOW_) return ["LAST_REFRESH_DISPATCH_ATTEMPT_DATE", "LAST_REFRESH_DISPATCH_ATTEMPT_AT"];
  return ["LAST_SETTLEMENT_DISPATCH_ATTEMPT_DATE", "LAST_SETTLEMENT_DISPATCH_ATTEMPT_AT"];
}

function dispatchWorkflow_(properties, workflow, clock) {
  var owner = encodeURIComponent(requiredProperty_(properties, "GITHUB_OWNER"));
  var repo = encodeURIComponent(requiredProperty_(properties, "GITHUB_REPO"));
  var token = requiredProperty_(properties, "GITHUB_TOKEN");
  var endpoint = "https://api.github.com/repos/" + owner + "/" + repo + "/actions/workflows/" + encodeURIComponent(workflow) + "/dispatches";
  var response;
  try {
    response = UrlFetchApp.fetch(endpoint, {
      method: "post",
      contentType: "application/json",
      headers: {
        Authorization: "Bearer " + token,
        Accept: "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
      },
      payload: JSON.stringify({ ref: "main", inputs: { target_date: clock.date } }),
      muteHttpExceptions: true,
    });
  } catch (error) {
    var attemptKeys = dispatchAttemptStateKeys_(workflow);
    properties.setProperty(attemptKeys[0], clock.date);
    properties.setProperty(attemptKeys[1], String(clock.nowMs));
    throw error;
  }
  if (response.getResponseCode() !== 204) {
    throw new Error("GitHub workflow dispatch failed with HTTP " + response.getResponseCode());
  }
  var keys = dispatchStateKeys_(workflow);
  properties.setProperty(keys[0], clock.date);
  properties.setProperty(keys[1], String(clock.nowMs));
}

function fetchImage_(properties, buildId) {
  var url = requiredProperty_(properties, "REPORT_IMAGE_URL") + "?build_id=" + encodeURIComponent(buildId);
  var response = UrlFetchApp.fetch(url, { muteHttpExceptions: true });
  if (response.getResponseCode() !== 200) return { bytes: [], blob: null, reason: "image HTTP " + response.getResponseCode() };
  var blob = response.getBlob();
  var bytes = blob.getBytes();
  if (!bytes || bytes.length === 0) return { bytes: [], blob: null, reason: "image bytes empty" };
  if (typeof blob.setName === "function") blob.setName("daily-report.png");
  return { bytes: bytes, blob: blob, reason: "" };
}

function sendNormalReport_(properties, clock, imageBlob, imageSha256) {
  var recipient = requiredProperty_(properties, "RECIPIENT_EMAIL");
  var siteUrl = requiredProperty_(properties, "REPORT_SITE_URL");
  var subject = "Daily report " + clock.date;
  var body = "The verified daily report is attached. Dashboard: " + siteUrl;
  var options = {
    htmlBody: "<p>The verified daily report is attached.</p><p><a href=\"" + siteUrl + "\">Open dashboard</a></p>",
    attachments: [imageBlob],
  };
  if (properties.getProperty("TEST_MODE") === "true") {
    Logger.log("TEST_MODE normal report send for " + clock.date);
  } else {
    GmailApp.sendEmail(recipient, subject, body, options);
    properties.setProperty("LAST_SENT_DATE", clock.date);
    properties.setProperty("LAST_SENT_IMAGE_SHA256", imageSha256);
  }
}

function escapeHtml_(value) {
  return String(value).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/\"/g, "&quot;");
}

function sendFailureNotice_(properties, clock, reasons, status) {
  var recipient = requiredProperty_(properties, "RECIPIENT_EMAIL");
  var siteUrl = requiredProperty_(properties, "REPORT_SITE_URL");
  var subject = "Daily report unavailable " + clock.date;
  var detail = reasons.length ? reasons.join("; ") : "report incomplete";
  var generatedAt = status && isFinite(timestampMillis_(status.generated_at_bjt)) ? status.generated_at_bjt : "unavailable";
  var body = "The daily report was not ready by 18:00 Beijing time. " + detail +
    ". Last generated at (Beijing): " + generatedAt + ". Dashboard: " + siteUrl;
  var options = {
    htmlBody: "<p>The daily report was not ready by 18:00 Beijing time.</p><p>" + escapeHtml_(detail) +
      "</p><p>Last generated at (Beijing): " + escapeHtml_(generatedAt) +
      "</p><p><a href=\"" + escapeHtml_(siteUrl) + "\">Open dashboard</a></p>",
  };
  if (properties.getProperty("TEST_MODE") === "true") {
    Logger.log("TEST_MODE failure notice send for " + clock.date);
  } else {
    GmailApp.sendEmail(recipient, subject, body, options);
    properties.setProperty("LAST_FAILURE_NOTICE_DATE", clock.date);
  }
}

function uniqueReasons_(reasons) {
  var seen = {};
  return reasons.filter(function (reason) {
    if (seen[reason]) return false;
    seen[reason] = true;
    return true;
  });
}

function tryVerifiedSend_(properties, clock, status) {
  var preliminary = reportReadiness_(status, clock.date, status && status.image_sha256);
  if (!preliminary.ready) return { sent: false, reasons: preliminary.reasons };
  var image;
  try {
    image = fetchImage_(properties, status.build_id);
  } catch (error) {
    return { sent: false, reasons: ["image fetch failed: " + String(error && error.message ? error.message : error)] };
  }
  if (!image.bytes.length) return { sent: false, reasons: [image.reason || "image bytes empty"] };
  var computedHash = sha256Hex_(image.bytes);
  var readiness = reportReadiness_(status, clock.date, computedHash);
  if (!readiness.ready) return { sent: false, reasons: readiness.reasons };
  sendNormalReport_(properties, clock, image.blob, computedHash);
  return { sent: true, reasons: [] };
}

function runAutomation() {
  var lock = LockService.getScriptLock();
  if (!lock.tryLock(5000)) return;
  try {
    var properties = PropertiesService.getScriptProperties();
    var clock = beijingClock_(new Date());
    var state = properties.getProperties();
    if (state.LAST_FAILURE_NOTICE_DATE === clock.date || state.LAST_SENT_DATE === clock.date) return;

    var fetched = fetchStatus_(properties, clock);
    var status = fetched.status;
    if (clock.minutes >= 18 * 60) {
      var finalAttempt = status ? tryVerifiedSend_(properties, clock, status) : { sent: false, reasons: fetched.reasons };
      if (!finalAttempt.sent) sendFailureNotice_(properties, clock, uniqueReasons_(fetched.reasons.concat(finalAttempt.reasons)), status);
      return;
    }

    var workflow = chooseDispatch_(clock, status, state);
    if (workflow) dispatchWorkflow_(properties, workflow, clock);

    if (clock.minutes >= 14 * 60 && status) tryVerifiedSend_(properties, clock, status);
  } finally {
    lock.releaseLock();
  }
}

function sendDailyReport() {
  return runAutomation();
}

function installAutomationTrigger() {
  ScriptApp.getProjectTriggers().forEach(function (trigger) {
    var handler = trigger.getHandlerFunction();
    if (handler === "runAutomation" || handler === "sendDailyReport") ScriptApp.deleteTrigger(trigger);
  });
  ScriptApp.newTrigger("runAutomation").timeBased().everyMinutes(10).create();
}
