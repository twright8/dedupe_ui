/* ============================================================
   useRunProgress — SSE hook for live run progress
   ============================================================ */

import { useState, useEffect, useRef } from "react";

export function useRunProgress(runId) {
  const [events, setEvents] = useState([]);
  const [currentStage, setCurrentStage] = useState(null);
  const [status, setStatus] = useState("connecting");
  const [isConnected, setIsConnected] = useState(false);
  const esRef = useRef(null);

  useEffect(() => {
    if (!runId) return;

    const url = `/api/runs/${runId}/progress`;
    const es = new EventSource(url);
    esRef.current = es;

    es.onopen = () => {
      setIsConnected(true);
      setStatus("connected");
    };

    es.onmessage = (evt) => {
      try {
        const data = JSON.parse(evt.data);
        setEvents((prev) => [...prev, data]);

        if (data.stage !== undefined && data.stage !== null) {
          setCurrentStage(data.stage);
        }
        if (data.status) {
          setStatus(data.status);
        }
        if (data.status === "complete" || data.status === "failed") {
          es.close();
          setIsConnected(false);
        }
      } catch {
        // non-JSON event — append as raw text
        setEvents((prev) => [...prev, { raw: evt.data }]);
      }
    };

    es.onerror = () => {
      setIsConnected(false);
      setStatus("disconnected");
      es.close();
    };

    return () => {
      es.close();
      setIsConnected(false);
    };
  }, [runId]);

  return { events, currentStage, status, isConnected };
}
