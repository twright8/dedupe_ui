/* ============================================================
   usePipelineStages — the stages, by name, fetched once
   ------------------------------------------------------------
   GET /api/pipeline/stages is the one list of stages. Every screen
   that names a stage reads it from here, so the new-run preview,
   the live progress panel, the run timeline and How it works can
   never drift apart.

   Stages are named and never numbered (docs/DESIGN.md D21). The
   stage KEY is the handle; the label is what a reader sees. An
   event that carries only a number gets no name, because guessing
   one would be worse than saying nothing.
   ============================================================ */

import { useState, useEffect } from "react";
import { api } from "../api";

let cache = null;
let inFlight = null;

function load() {
  if (cache) return Promise.resolve(cache);
  if (!inFlight) {
    inFlight = api
      .pipelineStages()
      .then((data) => {
        cache = Array.isArray(data) ? data : [];
        return cache;
      })
      .catch(() => {
        cache = [];
        return cache;
      })
      .finally(() => {
        inFlight = null;
      });
  }
  return inFlight;
}

export function usePipelineStages() {
  const [stages, setStages] = useState(cache);

  useEffect(() => {
    let alive = true;
    load().then((list) => alive && setStages(list));
    return () => {
      alive = false;
    };
  }, []);

  // A stage's own name, or null when nothing names it.
  function stageName(key) {
    if (key == null || key === "") return null;
    const found = (stages || []).find((s) => s.key === String(key));
    return found ? found.label || found.key : null;
  }

  return { stages, stageName };
}
