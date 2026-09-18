/* ============================================================
   Auth UI — LoginScreen + UserPicker
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "./api";
import { useProfile } from "./profile";

// ---------- LoginScreen ----------

export function LoginScreen({ onLogin }) {
  const profile = useProfile();
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e) {
    e.preventDefault();
    setError("");
    setLoading(true);
    try {
      await api.login({ password });
      onLogin();
    } catch (err) {
      setError("Invalid password. Please try again.");
    } finally {
      setLoading(false);
    }
  }

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
      <form onSubmit={handleSubmit} className="card" style={{ width: 340 }}>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", alignItems: "center", gap: 20, padding: "32px 28px" }}>
          <div className="brand-mark" style={{ width: 44, height: 44, fontSize: 20 }}>TI</div>
          <div style={{ textAlign: "center" }}>
            <h1 style={{ margin: 0, fontSize: 18, fontWeight: 600, letterSpacing: "-0.01em" }}>
              {profile.title}
            </h1>
            {profile.subtitle && (
              <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
                {profile.subtitle}
              </p>
            )}
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 12.5 }}>
              Sign in to continue
            </p>
          </div>
          <div className="field" style={{ width: "100%" }}>
            <label htmlFor="login-pw">Password</label>
            <input
              id="login-pw"
              className="input"
              type="password"
              placeholder="Enter password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
              autoFocus
            />
          </div>
          {error && (
            <p style={{ margin: 0, fontSize: 12.5, color: "var(--ti-red)", textAlign: "center" }}>
              {error}
            </p>
          )}
          <button
            type="submit"
            className="btn primary lg"
            disabled={loading || !password}
            style={{ width: "100%" }}
          >
            {loading ? "Signing in..." : "Sign in"}
          </button>
        </div>
      </form>
    </div>
  );
}

// ---------- UserPicker ----------

export function UserPicker({ onUserSet }) {
  const [users, setUsers] = useState([]);
  const [loading, setLoading] = useState(true);
  const [showAdd, setShowAdd] = useState(false);
  const [name, setName] = useState("");
  const [initials, setInitials] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listUsers()
      .then((data) => setUsers(Array.isArray(data) ? data : []))
      .catch(() => setUsers([]))
      .finally(() => setLoading(false));
  }, []);

  async function selectUser(user) {
    setSaving(true);
    setError("");
    try {
      await api.setUser({ name: user.name, initials: user.initials });
      onUserSet(user);
    } catch (err) {
      setError("Failed to set user. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  async function handleAdd(e) {
    e.preventDefault();
    if (!name.trim() || !initials.trim()) return;
    setSaving(true);
    setError("");
    try {
      await api.setUser({ name: name.trim(), initials: initials.trim().toUpperCase() });
      onUserSet({ name: name.trim(), initials: initials.trim().toUpperCase() });
    } catch (err) {
      setError("Failed to create user. Please try again.");
    } finally {
      setSaving(false);
    }
  }

  // Auto-generate initials from name
  function handleNameChange(val) {
    setName(val);
    const parts = val.trim().split(/\s+/);
    if (parts.length >= 2) {
      setInitials((parts[0][0] + parts[parts.length - 1][0]).toUpperCase());
    } else if (parts.length === 1 && parts[0].length >= 2) {
      setInitials(parts[0].substring(0, 2).toUpperCase());
    }
  }

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
      <div className="card" style={{ width: 380 }}>
        <div className="card-b" style={{ display: "flex", flexDirection: "column", gap: 16, padding: "28px 24px" }}>
          <div style={{ textAlign: "center" }}>
            <h2 style={{ margin: 0, fontSize: 17, fontWeight: 600, letterSpacing: "-0.01em" }}>
              Who is working?
            </h2>
            <p className="muted" style={{ margin: "4px 0 0", fontSize: 13 }}>
              Select your identity for audit trail
            </p>
          </div>

          {loading ? (
            <p className="muted pulse" style={{ textAlign: "center", fontSize: 13 }}>
              Loading users...
            </p>
          ) : (
            <div style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              {users.map((u) => (
                <button
                  key={u.name}
                  className="card"
                  onClick={() => selectUser(u)}
                  disabled={saving}
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 12,
                    padding: "10px 14px",
                    cursor: "pointer",
                    border: "1px solid var(--line)",
                    borderRadius: "var(--r-md)",
                    background: "var(--surface)",
                    textAlign: "left",
                    width: "100%",
                    transition: "background 80ms",
                  }}
                  onMouseEnter={(e) => (e.currentTarget.style.background = "var(--surface-sub)")}
                  onMouseLeave={(e) => (e.currentTarget.style.background = "var(--surface)")}
                >
                  <div className="avatar">{u.initials}</div>
                  <span style={{ fontWeight: 500, fontSize: 13.5 }}>{u.name}</span>
                </button>
              ))}
            </div>
          )}

          {error && (
            <p style={{ margin: 0, fontSize: 12.5, color: "var(--ti-red)", textAlign: "center" }}>
              {error}
            </p>
          )}

          {!showAdd ? (
            <button
              className="btn ghost"
              onClick={() => setShowAdd(true)}
              style={{ alignSelf: "center", fontSize: 13, color: "var(--muted)" }}
            >
              + Add new user
            </button>
          ) : (
            <form onSubmit={handleAdd} style={{ display: "flex", flexDirection: "column", gap: 10 }}>
              <hr className="rule" style={{ margin: "4px 0" }} />
              <div className="field">
                <label htmlFor="add-name">Full name</label>
                <input
                  id="add-name"
                  className="input"
                  placeholder="e.g. Tom Wright"
                  value={name}
                  onChange={(e) => handleNameChange(e.target.value)}
                  autoFocus
                />
              </div>
              <div className="field">
                <label htmlFor="add-initials">Initials (2 chars)</label>
                <input
                  id="add-initials"
                  className="input"
                  placeholder="e.g. TW"
                  maxLength={3}
                  value={initials}
                  onChange={(e) => setInitials(e.target.value.toUpperCase())}
                />
              </div>
              <div style={{ display: "flex", gap: 8 }}>
                <button
                  type="button"
                  className="btn"
                  onClick={() => {
                    setShowAdd(false);
                    setName("");
                    setInitials("");
                  }}
                  style={{ flex: 1 }}
                >
                  Cancel
                </button>
                <button
                  type="submit"
                  className="btn primary"
                  disabled={saving || !name.trim() || !initials.trim()}
                  style={{ flex: 1 }}
                >
                  {saving ? "Saving..." : "Add & select"}
                </button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
