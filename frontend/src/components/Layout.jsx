/* ============================================================
   Layout components — Brand, SidebarNav, Topbar
   Converted from mockup onClick routing to React Router NavLink
   ============================================================ */

import { useState, useEffect } from "react";
import { NavLink, useLocation, useParams } from "react-router-dom";
import { Icons } from "./Icons";
import { useProfile } from "../profile";
import { api } from "../api";

// ---------- Brand block ----------
export function Brand() {
  const profile = useProfile();
  return (
    <div className="brand">
      <div className="brand-mark">TI</div>
      <div className="brand-text">
        <span className="org">Transparency Intl. UK</span>
        <span className="tool" title={profile.subtitle}>{profile.title}</span>
      </div>
    </div>
  );
}

// ---------- Density / Theme toggle ----------
function PrefsToggle() {
  const [density, setDensity] = useState(
    () => localStorage.getItem("density") || "dense"
  );
  const [theme, setTheme] = useState(
    () => localStorage.getItem("theme") || "light"
  );

  function toggleDensity() {
    const next = density === "dense" ? "comfortable" : "dense";
    setDensity(next);
    localStorage.setItem("density", next);
    document.body.classList.remove("density-dense", "density-comfortable");
    document.body.classList.add(`density-${next}`);
  }

  function toggleTheme() {
    const next = theme === "light" ? "dark" : "light";
    setTheme(next);
    localStorage.setItem("theme", next);
    if (next === "dark") {
      document.body.classList.add("theme-dark");
    } else {
      document.body.classList.remove("theme-dark");
    }
  }

  return (
    <div style={{ display: "flex", gap: 4 }}>
      <button
        className="btn sm ghost"
        onClick={toggleDensity}
        title={`Density: ${density}`}
        style={{ fontSize: 11, padding: "0 6px" }}
      >
        {density === "dense" ? "Dense" : "Comfy"}
      </button>
      <button
        className="btn sm ghost"
        onClick={toggleTheme}
        title={`Theme: ${theme}`}
        style={{ fontSize: 11, padding: "0 6px" }}
      >
        {theme === "light" ? "Light" : "Dark"}
      </button>
    </div>
  );
}

// ---------- Sidebar navigation ----------
const navItems = [
  { to: "/methodology",  label: "How it works",    icon: Icons.spark },
  { group: "Workflow" },
  { to: "/runs",         label: "Runs",           icon: Icons.runs },
  { to: "/runs/new",     label: "New run",         icon: Icons.upload },
  { group: "Review" },
  { to: "/review",       label: "Review queue",    icon: Icons.review },
  { to: "/clusters",     label: "Cluster review",  icon: Icons.ambiguous, count: "reviewQueue" },
  { group: "Config" },
  { to: "/labels",       label: "Label library",   icon: Icons.review },
  { to: "/config",       label: "Config & rules",  icon: Icons.config },
  { to: "/audit",        label: "Audit log",       icon: Icons.audit },
];

export function SidebarNav({ user, onLogout }) {
  const displayName = user?.name || "Unknown";
  const displayInitials = user?.initials || "??";
  // The cluster queue is the only badge in the sidebar, because it is the one
  // number that says there is work waiting for a person.
  const [badges, setBadges] = useState({});

  useEffect(() => {
    let alive = true;
    api
      .listRuns()
      .then((data) => {
        if (!alive) return;
        const runs = Array.isArray(data) ? data : data.runs || [];
        const latest = runs.find((r) => r.status === "complete" && r.counts?.hasEntities);
        setBadges({ reviewQueue: latest?.counts?.reviewQueue || 0 });
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, []);

  return (
    <aside className="sidebar">
      <Brand />
      <nav className="nav">
        {navItems.map((it, i) =>
          it.group ? (
            <div key={"g" + i} className="nav-group nav-label">{it.group}</div>
          ) : (
            <NavLink
              key={it.to}
              to={it.to}
              end={it.to === "/runs"}
              className={({ isActive }) => "nav-item" + (isActive ? " active" : "")}
            >
              <it.icon size={16} />
              <span>{it.label}</span>
              {it.count && badges[it.count] > 0 && (
                <span className="count">{badges[it.count]}</span>
              )}
            </NavLink>
          )
        )}
      </nav>
      <div className="sidebar-foot" style={{ flexDirection: "column", alignItems: "stretch", gap: 8 }}>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <div className="avatar">{displayInitials}</div>
          <div style={{ display: "flex", flexDirection: "column", lineHeight: 1.2, flex: 1, minWidth: 0 }}>
            <span style={{ color: "var(--ink)", fontWeight: 500, fontSize: 12.5, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
              {displayName}
            </span>
            <span style={{ fontSize: 11 }}>Analyst</span>
          </div>
          {onLogout && (
            <button
              className="btn sm ghost icon"
              onClick={onLogout}
              title="Log out"
              aria-label="Log out"
              style={{ marginLeft: "auto" }}
            >
              <Icons.export size={14} />
            </button>
          )}
        </div>
        <PrefsToggle />
      </div>
    </aside>
  );
}

// ---------- Topbar with breadcrumbs ----------
export function Topbar({ crumbs, right }) {
  return (
    <div className="topbar">
      <div className="crumbs">
        {crumbs.map((c, i) => (
          <span key={i}>
            {i > 0 && <span className="sep"> / </span>}
            <span className={i === crumbs.length - 1 ? "cur" : ""}>{c}</span>
          </span>
        ))}
      </div>
      <div className="topbar-right">{right}</div>
    </div>
  );
}
