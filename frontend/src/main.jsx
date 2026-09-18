/* ============================================================
   Entry point — mount React app with Router
   ============================================================ */

import React from "react";
import ReactDOM from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { ProfileProvider } from "./profile";
import "./styles/index.css";

// window.__BASE__ is injected by the backend (e.g. "/donations"); in dev it is
// undefined, which means the app is served from the root.
ReactDOM.createRoot(document.getElementById("root")).render(
  <React.StrictMode>
    <BrowserRouter basename={window.__BASE__ || ""}>
      <ProfileProvider>
        <App />
      </ProfileProvider>
    </BrowserRouter>
  </React.StrictMode>
);
