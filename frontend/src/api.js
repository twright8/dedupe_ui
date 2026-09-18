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
    throw new Error(`${method} ${path} ${res.status}: ${text}`);
  }
  if (res.status === 204) return null;
  return res.json();
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
  labelsExportUrl() {
    return apiUrl("/api/labels/export.csv");
  },
  importLabelsCsv(csvText) {
    return post("/api/labels/import-csv", { csv: csvText });
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
  addJurisdictions(data) {
    return post("/api/config/jurisdictions", data);
  },
  testRules(data) {
    return post("/api/config/test-rules", data);
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
