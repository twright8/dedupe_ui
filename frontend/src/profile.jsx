/* ============================================================
   Profile context — which tool this instance is
   ------------------------------------------------------------
   One codebase runs as several instances (donations, PSC, ...). The
   profile carries the naming, the input file description, the two
   tracks and the record columns. It is fetched once at startup and
   needs no login, because the login screen already shows the title.
   Children only render once it has loaded, so useProfile() never
   returns null.
   ============================================================ */

import { createContext, useContext, useEffect, useState } from "react";
import { api } from "./api";
import { Empty } from "./components/Empty";

const ProfileContext = createContext(null);

export function useProfile() {
  return useContext(ProfileContext);
}

export function ProfileProvider({ children }) {
  const [profile, setProfile] = useState(null);
  const [error, setError] = useState(null);
  const [attempt, setAttempt] = useState(0);

  useEffect(() => {
    let alive = true;
    api
      .getProfile()
      .then((data) => {
        if (alive) {
          setProfile(data);
          setError(null);
        }
      })
      .catch((err) => {
        if (alive) setError(err.message);
      });
    return () => {
      alive = false;
    };
  }, [attempt]);

  // The browser tab carries the tool name too, so two instances open
  // side by side are told apart.
  useEffect(() => {
    if (profile) {
      document.title = [profile.title, profile.subtitle, "TI UK"]
        .filter(Boolean)
        .join(" - ");
    }
  }, [profile]);

  if (error) {
    return (
      <div
        style={{
          minHeight: "100vh",
          display: "grid",
          placeItems: "center",
          background: "var(--bg)",
          padding: 20,
        }}
      >
        <Empty
          title="Could not load this tool's profile"
          sub={error}
          action={
            <button className="btn primary" onClick={() => setAttempt((n) => n + 1)}>
              Retry
            </button>
          }
        />
      </div>
    );
  }

  if (!profile) {
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

  return (
    <ProfileContext.Provider value={profile}>{children}</ProfileContext.Provider>
  );
}
