/* ============================================================
   useLabels — optimistic label state with API persistence
   ============================================================ */

import { useState, useCallback, useRef } from "react";
import { api } from "../api";

/**
 * Manages label state with optimistic updates.
 * Tracks per-match labels locally. On setLabel(), updates local state
 * immediately and fires api.createLabel(). If the API call fails,
 * reverts the optimistic update.
 *
 * @param {Array} initialItems — match items (used to seed labels from existing label data)
 * @returns {{ labels: Object, notes: Object, setLabel: Function, setNote: Function, pending: Set }}
 */
export function useLabels(initialItems) {
  // Build initial labels map from any pre-existing label data on items
  const [labels, setLabels] = useState(() => {
    const map = {};
    if (initialItems) {
      for (const item of initialItems) {
        if (item.label) {
          map[item.id || item.match_id] = item.label;
        }
      }
    }
    return map;
  });

  const [notes, setNotes] = useState({});

  // Set of match IDs currently being persisted
  const [pending, setPending] = useState(new Set());
  const pendingRef = useRef(new Set());

  const setNote = useCallback((matchId, value) => {
    setNotes(prev => ({ ...prev, [matchId]: value }));
  }, []);

  /**
   * Label a match with optimistic update.
   * @param {string} matchId
   * @param {string|null} value — "TRUE", "FALSE", or null to clear
   * @param {string} noteText — reviewer notes
   * @param {Object} rawData — the full match record (needed for API body)
   */
  const setLabel = useCallback((matchId, value, noteText, rawData) => {
    // Capture previous value for rollback
    setLabels(prev => {
      const prevValue = prev[matchId];

      // Optimistic update
      const next = { ...prev };
      if (value === null || value === undefined) {
        delete next[matchId];
      } else {
        next[matchId] = value;
      }

      // If clearing or no raw data for API call, just update locally
      if (value === null || value === undefined || !rawData) {
        return next;
      }

      // Fire API call in background
      pendingRef.current = new Set([...pendingRef.current, matchId]);
      setPending(new Set(pendingRef.current));

      api
        .createLabel({
          ocod_name_clean: rawData.ocod_name_clean || rawData.ocod_name_raw,
          jurisdiction_clean: rawData.jurisdiction_clean,
          roe_company_number: rawData.roe_company_number,
          roe_name: rawData.roe_name_raw || rawData.roe_name,
          ocod_name_raw: rawData.ocod_name_raw,
          ocod_jurisdiction_raw: rawData.ocod_jurisdiction_raw || rawData.jurisdiction_clean,
          is_true_match: value,
          reviewer_notes: noteText || "",
          run_id: rawData.run_id,
        })
        .then(() => {
          pendingRef.current = new Set(
            [...pendingRef.current].filter(id => id !== matchId)
          );
          setPending(new Set(pendingRef.current));
        })
        .catch(() => {
          // Revert optimistic update
          pendingRef.current = new Set(
            [...pendingRef.current].filter(id => id !== matchId)
          );
          setPending(new Set(pendingRef.current));
          setLabels(revert => {
            const r = { ...revert };
            if (prevValue === null || prevValue === undefined) {
              delete r[matchId];
            } else {
              r[matchId] = prevValue;
            }
            return r;
          });
        });

      return next;
    });
  }, []);

  return { labels, notes, setLabel, setNote, pending };
}
