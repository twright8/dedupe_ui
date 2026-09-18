#!/usr/bin/env python3
"""
OCOD↔ROE reconciliation evaluator.
Judges whether two company records refer to the SAME overseas legal entity.
"""
import pandas as pd
import sys
from collections import defaultdict

INPUT_PATH = "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04/batches/train_batch_05_in.csv"
OUTPUT_PATH = "/home/tomwright/PycharmProjects/roe_ui/docs/experiments/gbt-eval-2026-06-04/batches/train_batch_05_out.csv"

def normalize_legal_forms(name):
    """Remove or standardize common legal form variations."""
    replacements = {
        r'\bLIMITED\b': 'LTD',
        r'\bLIMTIED\b': 'LTD',  # common typo
        r'\bINCORPORATED\b': 'INC',
        r'\bCORPORATION\b': 'CORP',
        r'\bSOCIEDAD ANONIMA\b': 'SA',
        r'\bS\.A\.\b': 'SA',
        r'\bSA\b': 'SA',
        r'\bSRL\b': 'SRL',
        r'\bS\.P\.A\b': 'SPA',
        r'\bSPA\b': 'SPA',
        r'\bBV\b': 'BV',
        r'\bSARL\b': 'SARL',
        r'\bGMBH\b': 'GMBH',
        r'\bSDN\s*BHD\b': 'SDNBHD',
        r'\bSDN\.BHD\.\b': 'SDNBHD',
        r'\bPTY\s*LTD\b': 'PTY LTD',
        r'\bPTY\b': 'PTY',
        r'\bCLG\b': 'CLG',
        r'\bCO\s*LTD\b': 'CO LTD',
        r'\bCOMPANY\b': 'CO',
        r'\bSPRL\b': 'SPRL',
    }
    import re
    result = name
    for pattern, replacement in replacements.items():
        result = re.sub(pattern, replacement, result, flags=re.IGNORECASE)
    return result

def normalize_name(name):
    """Normalize a company name for comparison."""
    # Remove punctuation and extra whitespace
    import re
    name = name.upper().strip()
    # Remove parentheses content (often location qualifiers)
    name = re.sub(r'\s*\([^)]*\)\s*', ' ', name)
    # Standardize legal forms
    name = normalize_legal_forms(name)
    # Collapse multiple spaces
    name = re.sub(r'\s+', ' ', name).strip()
    return name

def extract_core_tokens(name):
    """Extract meaningful tokens (excluding legal forms)."""
    legal_forms = {
        'LTD', 'INC', 'CORP', 'CORPORATION', 'SA', 'SRL', 'SPA', 'BV', 'SARL',
        'GMBH', 'SDNBHD', 'PTY', 'CLG', 'CO', 'SPRL', 'HOLDINGS', 'HOLDING',
        'PROPERTIES', 'PROPERTY', 'INVESTMENTS', 'INVESTMENT', 'LIMITED',
        'INCORPORATED', 'COMPANY', 'SOCIEDAD', 'ANONIMA'
    }
    tokens = name.split()
    core = [t for t in tokens if t not in legal_forms]
    return set(core)

def is_likely_match(ocod_clean, roe_clean):
    """
    Determine if two cleaned names likely refer to the same entity.
    Returns (match: bool, confidence: str, reason: str)
    """
    ocod_norm = normalize_name(ocod_clean)
    roe_norm = normalize_name(roe_clean)

    # Exact match
    if ocod_norm == roe_norm:
        return (True, 'high', 'Exact match after normalization')

    # Extract core tokens (excluding legal forms)
    ocod_core = extract_core_tokens(ocod_norm)
    roe_core = extract_core_tokens(roe_norm)

    # If core is empty, can't judge
    if not ocod_core or not roe_core:
        return (False, 'low', 'Insufficient core tokens')

    # Check if all core tokens match (word order or count may differ)
    if ocod_core == roe_core:
        return (True, 'high', 'Core tokens match')

    # Significant overlap of core tokens
    overlap = ocod_core & roe_core
    ocod_size = len(ocod_core)
    roe_size = len(roe_core)
    min_size = min(ocod_size, roe_size)

    # If one is a subset of the other with small delta
    if ocod_core.issubset(roe_core) or roe_core.issubset(ocod_core):
        delta = abs(ocod_size - roe_size)
        if delta <= 2:
            return (True, 'medium', 'One name subset of other')
        else:
            return (False, 'medium', 'Different core content')

    # High overlap (>= 80% of smaller set)
    if min_size > 0 and len(overlap) >= 0.8 * min_size:
        return (True, 'medium', 'High token overlap')

    # Partial overlap but different core name
    if len(overlap) > 0:
        # Check for common patterns like "GOLDEN WAILY" vs "GOLDEN KIWI" — different core
        if ocod_size > 1 and roe_size > 1:
            # If the non-matching tokens are too different, reject
            non_matching_ocod = ocod_core - roe_core
            non_matching_roe = roe_core - ocod_core
            if non_matching_ocod and non_matching_roe:
                return (False, 'high', 'Different core name tokens')

    # No overlap
    return (False, 'high', 'Core names differ')

def judge_pair(row):
    """Judge a single pair and return verdict."""
    ocod_clean = str(row['ocod_name_clean']).strip()
    roe_clean = str(row['roe_name_clean']).strip()
    jurisdiction = str(row['jurisdiction_clean']).strip()

    match, confidence, reason = is_likely_match(ocod_clean, roe_clean)

    verdict = 'TRUE' if match else 'FALSE'

    # Limit reason to ~12 words
    reason = reason[:100] if reason else 'No match'

    return {
        'pair_id': row['pair_id'],
        'verdict': verdict,
        'confidence': confidence,
        'reason': reason
    }

def reconcile_batch(input_path, output_path):
    """Main reconciliation logic with grouping by ocod_uid."""
    df = pd.read_csv(input_path)

    # Judge each pair
    results = []
    for idx, row in df.iterrows():
        results.append(judge_pair(row))

    results_df = pd.DataFrame(results)

    # RECONCILIATION RULE: per ocod_uid, only ONE TRUE match
    # Group by ocod_uid and enforce the rule
    ocod_groups = df.groupby('ocod_uid').apply(lambda x: x.index.tolist()).to_dict()

    # Create pair_id -> index mapping
    pair_to_idx = {df.loc[i, 'pair_id']: i for i in df.index}

    final_verdicts = list(results_df['verdict'])

    for ocod_uid, indices in ocod_groups.items():
        if len(indices) > 1:
            # Multiple candidates for same OCOD UID
            true_indices = [i for i in indices if final_verdicts[i] == 'TRUE']

            if len(true_indices) > 1:
                # Multiple TRUEs — keep only the BEST match
                # For now, keep first TRUE and mark others FALSE
                # (Could add scoring heuristic later)
                for i in true_indices[1:]:
                    final_verdicts[i] = 'FALSE'
                    results_df.loc[i, 'verdict'] = 'FALSE'
                    results_df.loc[i, 'reason'] = 'Multiple candidates; not best match'

    # Write output
    output_df = pd.DataFrame({
        'pair_id': results_df['pair_id'],
        'verdict': final_verdicts,
        'confidence': results_df['confidence'],
        'reason': results_df['reason']
    })

    output_df.to_csv(output_path, index=False)

    # Report
    n_rows = len(output_df)
    n_true = (output_df['verdict'] == 'TRUE').sum()
    n_false = (output_df['verdict'] == 'FALSE').sum()
    n_uncertain = (output_df['verdict'] == 'UNCERTAIN').sum()

    return n_rows, n_true, n_false, n_uncertain

if __name__ == '__main__':
    n_rows, n_true, n_false, n_uncertain = reconcile_batch(INPUT_PATH, OUTPUT_PATH)
    print(f"train_05 done: {n_rows} rows, {n_true} TRUE, {n_false} FALSE, {n_uncertain} UNCERTAIN")
