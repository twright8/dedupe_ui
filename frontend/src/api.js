/* ============================================================
   API client — thin fetch wrappers for /api/* endpoints
   ------------------------------------------------------------
   One build serves any URL prefix. In production the backend injects
   window.__BASE__ (e.g. "/donations") into index.html; in dev it is
   undefined, which means "no prefix". Every request and every URL we
   hand to the browser goes through apiUrl() so the prefix is applied
   in exactly one place.
   ============================================================ */

// Root-relative paths get the deploy prefix; anything else is left alone.
export function apiUrl(path) {
  const base = (typeof window !== "undefined" && window.__BASE__) || "";
  return path.startsWith("/") ? base + path : path;
}

async function request(method, path, body) {
  const opts = {
    method,
    headers: {},
  };
  if (body !== undefined) {
    opts.headers["Content-Type"] = "application/json";
    opts.body = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(path), opts);
  if (!res.ok) {
    const text = await res.text().catch(() => "");
    // The message stays as it always was, so existing alert() call sites read
    // the same. The parsed body rides along for callers that want the
    // structured detail — config validation is the one that does.
    const err = new Error(`${method} ${path} ${res.status}: ${text}`);
    err.status = res.status;
    try {
      err.body = JSON.parse(text);
    } catch {
      err.body = null;
    }
    throw err;
  }
  if (res.status === 204) return null;
  return res.json();
}

/* Validation errors out of a failed save. The backend answers 422 with
   {"detail": {"errors": [{path, message}]}}; older shapes and plain network
   failures give back an empty list, so callers can fall back to alert(). */
export function validationErrors(err) {
  const body = err?.body;
  const candidates = [body?.detail?.errors, body?.errors, body?.detail];
  for (const list of candidates) {
    if (Array.isArray(list) && list.every((e) => e && typeof e === "object")) {
      return list.filter((e) => e.path || e.message);
    }
  }
  return [];
}

function get(path) {
  return request("GET", path);
}
function post(path, body) {
  return request("POST", path, body);
}
function put(path, body) {
  return request("PUT", path, body);
}
function del(path) {
  return request("DELETE", path);
}

export const api = {
  // --- Profile (which tool this instance is; no login needed) ---
  getProfile() {
    return get("/api/profile");
  },

  // --- Auth ---
  login(credentials) {
    return post("/api/auth/login", credentials);
  },
  logout() {
    return post("/api/auth/logout");
  },
  me() {
    return get("/api/auth/me");
  },
  setUser(data) {
    return post("/api/auth/set-user", data);
  },
  listUsers() {
    return get("/api/auth/users");
  },

  // --- Runs ---
  listRuns(params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs${qs}`);
  },
  createRun(data) {
    return post("/api/runs", data);
  },
  getRun(id) {
    return get(`/api/runs/${id}`);
  },
  getRunFiles(id) {
    return get(`/api/runs/${id}/files`);
  },
  getRunRecords(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/records${qs}`);
  },
  getRunExactGroups(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/exact-groups${qs}`);
  },
  getRunExactGroup(id, groupId) {
    return get(`/api/runs/${id}/exact-groups/${encodeURIComponent(groupId)}`);
  },
  getRunExactEval(id) {
    return get(`/api/runs/${id}/exact-eval`);
  },

  // --- Scored pairs (stage 3) ---
  getRunPairs(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/pairs${qs}`);
  },
  // A pair id carries a bar between its two unit ids, so it is encoded here once.
  getRunPair(id, pairId) {
    return get(`/api/runs/${id}/pairs/${encodeURIComponent(pairId)}`);
  },
  getRunPairsHistogram(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/pairs/histogram${qs}`);
  },
  getRunScoreEval(id) {
    return get(`/api/runs/${id}/score-eval`);
  },
  getRunBlockingReport(id) {
    return get(`/api/runs/${id}/blocking-report`);
  },
  getRunContradictions(id) {
    return get(`/api/runs/${id}/contradictions`);
  },
  saveRunLabels(id, data) {
    return post(`/api/runs/${id}/labels`, data);
  },
  deleteRunLabel(id, pairId) {
    return del(`/api/runs/${id}/labels/${encodeURIComponent(pairId)}`);
  },

  getRunMatches(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/matches${qs}`);
  },
  getRunMatchesByOcod(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/matches/by-ocod${qs}`);
  },
  getRunMatchesByRoe(id, params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/runs/${id}/matches/by-roe${qs}`);
  },
  reBucketRun(id, data) {
    return post(`/api/runs/${id}/re-bucket`, data);
  },
  createLabelsBatch(data) {
    return post(`/api/labels/batch`, data);
  },
  getRunDiagnostics(id) {
    return get(`/api/runs/${id}/diagnostics`);
  },
  getRunTimeline(id) {
    return get(`/api/runs/${id}/timeline`);
  },
  cancelRun(id) {
    return post(`/api/runs/${id}/cancel`);
  },
  deleteRun(id) {
    return del(`/api/runs/${id}`);
  },
  applyLabels(id, data) {
    return post(`/api/runs/${id}/apply-labels`, data);
  },
  markUnlabelled(id, data) {
    return post(`/api/runs/${id}/mark-unlabelled`, data);
  },
  runFileUrl(id, filename) {
    return apiUrl(
      `/api/runs/${encodeURIComponent(id)}/files/${filename
        .split("/")
        .map(encodeURIComponent)
        .join("/")}`
    );
  },
  runAllFilesUrl(id) {
    return apiUrl(`/api/runs/${encodeURIComponent(id)}/files/all`);
  },

  // --- Labels ---
  listLabels(params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/labels${qs}`);
  },
  createLabel(data) {
    return post("/api/labels", data);
  },
  updateLabel(id, data) {
    return put(`/api/labels/${id}`, data);
  },
  deleteLabel(id) {
    return del(`/api/labels/${id}`);
  },
  setLabelsRole(ids, heldOut) {
    return post("/api/labels/role", { ids, held_out: heldOut ? 1 : 0 });
  },
  labelsExportUrl(params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return apiUrl(`/api/labels/export.csv${qs}`);
  },
  importLabels(csvText) {
    return post("/api/labels/import", { csv: csvText });
  },

  // --- Model (GBT) ---
  modelStatus() {
    return get("/api/model");
  },
  trainModel(runId) {
    return post("/api/model/train", { run_id: runId });
  },
  applyModel(runId) {
    return post("/api/model/apply", { run_id: runId });
  },
  activeBatch(runId, n) {
    return post("/api/model/active-batch", { run_id: runId, n: n || 50 });
  },
  importLabels(runId, results) {
    return post("/api/model/import-labels", { run_id: runId, results });
  },
  importModelLabelsCsv(runId, csvText) {
    return post("/api/model/import-labels-csv", { run_id: runId, csv: csvText });
  },
  explainPair(runId, pair) {
    return post("/api/model/explain", { run_id: runId, ...pair });
  },
  evalSetStatus() {
    return get("/api/model/eval-set");
  },
  designateEvalSet(n) {
    return post("/api/model/eval-set/designate", { n: n || 200 });
  },

  // --- Config ---
  currentConfig() {
    return get("/api/config/current");
  },
  listConfigVersions() {
    return get("/api/config/versions");
  },
  getConfigVersion(version) {
    return get(`/api/config/versions/${version}`);
  },
  diffConfig(fromV, toV) {
    return get(`/api/config/diff/${fromV}/${toV}`);
  },
  saveConfig(data) {
    return post("/api/config", data);
  },
  validateConfig(data) {
    return post("/api/config/validate", data);
  },
  configFunctions() {
    return get("/api/config/functions");
  },
  configColumns(data) {
    return post("/api/config/columns", data);
  },
  previewCleaning(data) {
    return post("/api/config/preview-cleaning", data);
  },
  previewDerived(data) {
    return post("/api/config/preview-derived", data);
  },
  previewKeys(data) {
    return post("/api/config/preview-keys", data);
  },
  previewTracks(data) {
    return post("/api/config/preview-tracks", data);
  },
  addLookupRows(table, data) {
    return post(`/api/config/lookups/${encodeURIComponent(table)}/rows`, data);
  },

  // --- Pipeline ---
  pipelineStages() {
    return get("/api/pipeline/stages");
  },

  // --- Shared notes ---
  getMethodologyNotes() {
    return get("/api/notes/methodology");
  },
  saveMethodologyNotes(content) {
    return put("/api/notes/methodology", { content });
  },

  // --- Audit ---
  listAudit(params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return get(`/api/audit${qs}`);
  },
  auditCounts() {
    return get("/api/audit/counts");
  },
  auditExportUrl(params) {
    const qs = params ? "?" + new URLSearchParams(params).toString() : "";
    return apiUrl(`/api/audit/export.jsonl${qs}`);
  },

  // --- Uploads ---
  uploadChunk(uploadId, formData) {
    return fetch(apiUrl("/api/uploads"), {
      method: "POST",
      body: formData,
    }).then((res) => {
      if (!res.ok) throw new Error(`Upload failed: ${res.status}`);
      return res.json();
    });
  },
  uploadStatus(uploadId) {
    return get(`/api/uploads/${uploadId}/status`);
  },
};
