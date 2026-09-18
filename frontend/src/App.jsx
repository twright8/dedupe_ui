/* ============================================================
   App shell — React Router routes + layout, wrapped in auth gate
   ============================================================ */

import { useState, useEffect } from "react";
import { Routes, Route, Navigate, useLocation } from "react-router-dom";
import { SidebarNav, Topbar } from "./components/Layout";
import { Icons } from "./components/Icons";
import { LoginScreen, UserPicker } from "./auth";
import { api } from "./api";
import RunsScreen from "./screens/RunsScreen";
import NewRunScreen from "./screens/NewRunScreen";
import RunDetailScreen from "./screens/RunDetailScreen";
import ReviewScreen from "./screens/ReviewScreen";
import AmbiguousScreen from "./screens/AmbiguousScreen";
import ConfigScreen from "./screens/ConfigScreen";
import LabelsScreen from "./screens/LabelsScreen";
import AuditScreen from "./screens/AuditScreen";
import MethodologyScreen from "./screens/MethodologyScreen";
import { Empty } from "./components/Empty";

// ---------- Placeholder page (replaced by real screens in later tasks) ----------
function Placeholder({ name }) {
  return (
    <div className="content">
      <div className="page-head">
        <div>
          <h1 className="page-title">{name}</h1>
          <p className="page-sub">This screen will be implemented in a later task.</p>
        </div>
      </div>
    </div>
  );
}

// ---------- Breadcrumb logic ----------
function useCrumbs() {
  const location = useLocation();
  const path = location.pathname;

  if (path === "/runs/new") return ["Linkage", "Runs", "New run"];
  if (path.match(/^\/runs\/[^/]+\/review$/)) return ["Linkage", "Runs", "Run", "Review queue"];
  if (path.match(/^\/runs\/[^/]+\/ambiguous$/)) return ["Linkage", "Runs", "Run", "Ambiguous"];
  if (path.match(/^\/runs\/[^/]+$/)) return ["Linkage", "Runs", "Run detail"];
  if (path === "/runs") return ["Linkage", "Runs"];
  if (path === "/review") return ["Linkage", "Review queue"];
  if (path === "/ambiguous") return ["Linkage", "Ambiguous"];
  if (path === "/labels") return ["Linkage", "Label library"];
  if (path === "/config") return ["Linkage", "Config & rules"];
  if (path === "/audit") return ["Linkage", "Audit log"];
  return ["Linkage"];
}

// ---------- Topbar right-side controls ----------
function TopbarRight() {
  return (
    <>
      <div className="search" style={{ width: 260 }}>
        <Icons.search size={14} />
        <input className="input" placeholder="Search runs, names, ROE ids..." />
      </div>
      <button className="btn"><Icons.refresh size={14} /></button>
      <button className="btn icon" title="Notifications"><Icons.alert size={14} /></button>
    </>
  );
}

function LatestRunRedirect({ target }) {
  const [state, setState] = useState({ loading: true, run: null, error: null });

  useEffect(() => {
    let alive = true;
    api
      .listRuns()
      .then((data) => {
        if (!alive) return;
        const runs = Array.isArray(data) ? data : data.runs || [];
        const bucket = target === "ambiguous" ? "ambiguous" : "review";
        const exact = runs.find((r) => r.status === "complete" && (r.counts?.[bucket] || 0) > 0);
        const fallback = runs.find((r) => r.status === "complete");
        setState({ loading: false, run: exact || fallback || null, error: null });
      })
      .catch((err) => {
        if (alive) setState({ loading: false, run: null, error: err.message });
      });
    return () => {
      alive = false;
    };
  }, [target]);

  if (state.loading) {
    return (
      <div className="content">
        <p className="muted pulse" style={{ fontSize: 13.5, padding: 40 }}>
          Finding latest complete run...
        </p>
      </div>
    );
  }

  if (!state.run) {
    return (
      <div className="content">
        <Empty
          title="No complete run found"
          sub={state.error || "Start a run before opening the global review links."}
        />
      </div>
    );
  }

  return <Navigate to={`/runs/${state.run.id}/${target}`} replace />;
}

// ---------- App layout wrapper ----------
function AppLayout({ user, onLogout }) {
  const crumbs = useCrumbs();

  return (
    <div className="app">
      <SidebarNav user={user} onLogout={onLogout} />
      <div className="main">
        <Topbar crumbs={crumbs} right={<TopbarRight />} />
        <Routes>
          <Route index element={<Navigate to="/runs" replace />} />
          <Route path="runs" element={<RunsScreen />} />
          <Route path="runs/new" element={<NewRunScreen />} />
          <Route path="runs/:id" element={<RunDetailScreen />} />
          <Route path="runs/:id/review" element={<ReviewScreen />} />
          <Route path="runs/:id/ambiguous" element={<AmbiguousScreen />} />
          <Route path="review" element={<LatestRunRedirect target="review" />} />
          <Route path="ambiguous" element={<LatestRunRedirect target="ambiguous" />} />
          <Route path="labels" element={<LabelsScreen />} />
          <Route path="config" element={<ConfigScreen />} />
          <Route path="audit" element={<AuditScreen />} />
          <Route path="methodology" element={<MethodologyScreen />} />
        </Routes>
      </div>
    </div>
  );
}

// ---------- Density / theme initializer ----------
function initPreferences() {
  const density = localStorage.getItem("density") || "dense";
  const theme = localStorage.getItem("theme") || "light";
  document.body.classList.remove("density-dense", "density-comfortable");
  document.body.classList.add(`density-${density}`);
  if (theme === "dark") {
    document.body.classList.add("theme-dark");
  } else {
    document.body.classList.remove("theme-dark");
  }
}

// ---------- Auth gate ----------
// States: "checking" | "login" | "pick-user" | "ready"
export default function App() {
  const [authState, setAuthState] = useState("checking");
  const [user, setUser] = useState(null);

  // Apply density + theme on mount
  useEffect(() => {
    initPreferences();
  }, []);

  // Check auth status on mount
  useEffect(() => {
    api
      .me()
      .then((data) => {
        if (data && data.name) {
          setUser(data);
          setAuthState("ready");
        } else {
          setAuthState("pick-user");
        }
      })
      .catch((err) => {
        if (err.message && err.message.includes("401")) {
          setAuthState("login");
        } else {
          // Network error or server down — show login
          setAuthState("login");
        }
      });
  }, []);

  function handleLogin() {
    // After login, check if user identity is set
    api
      .me()
      .then((data) => {
        if (data && data.name) {
          setUser(data);
          setAuthState("ready");
        } else {
          setAuthState("pick-user");
        }
      })
      .catch(() => {
        setAuthState("pick-user");
      });
  }

  function handleUserSet(userData) {
    setUser(userData);
    setAuthState("ready");
  }

  function handleLogout() {
    api
      .logout()
      .catch(() => {})
      .finally(() => {
        setUser(null);
        setAuthState("login");
      });
  }

  if (authState === "checking") {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "flex",
          alignItems: "center",
          justifyContent: "center",
          background: "var(--bg)",
        }}
      >
        <p className="muted pulse" style={{ fontSize: 13.5 }}>Loading...</p>
      </div>
    );
  }

  if (authState === "login") {
    return <LoginScreen onLogin={handleLogin} />;
  }

  if (authState === "pick-user") {
    return <UserPicker onUserSet={handleUserSet} />;
  }

  return <AppLayout user={user} onLogout={handleLogout} />;
}
