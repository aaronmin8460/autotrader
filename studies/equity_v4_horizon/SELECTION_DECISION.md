# Selection-set decision — recorded before any holdout cell was computed

**Timestamp:** 2026-08-30 (immediately after the 40th selection-set cell completed)

**Decision:** NO challenger horizon survives the frozen winner rule.

- h=8: 0 of 10 cells non-null → P1 fails (needs ≥4 and > h=4's count).
- h=16: 0 of 10 cells non-null → P1 fails.
- h=26: 0 of 10 cells non-null → P1 fails.
- h=4 (incumbent): 1 of 10 non-null (QQQ/2023-summer logistic). That model's
  isotonic calibration carries an extreme step (0.999839) supported by exactly
  ONE validation row, it emitted extreme predictions on scored bars, and it
  LOST to the raw null out of sample (log-loss gain −0.0177). Under P6 it does
  not count as a clean non-null.

Under the failure rule, the provisional classification is:

**CURRENT V4 HORIZON REMAINS MOST DEFENSIBLE** (classification B), with the
failure mode to be finalized in the report after the V5 diagnostic and the
overlap audits complete. The evidence so far points at
HORIZON_NOT_THE_PROBLEM with FEATURE_INFORMATION_LIMIT as the primary cause:
shadow logistic/GBM models score OOS ROC AUC ≈ 0.5 at every horizon, and
their OOS log-loss gains versus the null are overwhelmingly negative.

**Holdout status:** every 2026-summer cell at h∈{8,16,26} remains uncomputed
at this timestamp. Any holdout cell computed after this file exists is a
POST-DECISION DIAGNOSTIC for report completeness, not selection evidence.
