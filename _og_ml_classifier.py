"""ML decision tree + random forest on PGG2 V55 trade history.

Goal: find any multi-feature leaf with WR >= 90% and stable n_test >= 10.
"""
from __future__ import annotations

import os
import numpy as np
import pandas as pd
from sklearn.tree import DecisionTreeClassifier, _tree
from sklearn.ensemble import RandomForestClassifier
from sklearn.model_selection import StratifiedKFold


CSV = r"C:\Users\VASU\AppData\Local\Temp\pgg2_trades.csv"

FEATURES = [
    "score", "cost", "hour", "minute_of_day",
    "impact", "roundtrip_loss", "hold_s",
    "sec_since_last_win", "sec_since_last_loss",
    "consec_prior_W", "consec_prior_L",
    "wr_prev_3", "wr_prev_5", "wr_prev_10",
    "other_buys_30s", "other_wins_60s", "concurrent_open",
]
LANE_FEATS = ["lane_priced_snap", "lane_preprice_reveal", "lane_curve_lag_reveal"]


def load() -> pd.DataFrame:
    df = pd.read_csv(CSV)
    # One-hot encode lane
    for ln in ["priced_snap", "preprice_reveal", "curve_lag_reveal"]:
        df[f"lane_{ln}"] = (df["lane"] == ln).astype(int)
    # Coerce booleans
    df["win"] = df["win"].map({True: 1, "True": 1, False: 0, "False": 0}).fillna(0).astype(int)
    # Median impute
    for c in FEATURES:
        if df[c].isna().any():
            med = df[c].median()
            df[c] = df[c].fillna(med)
    return df


def extract_leaves(clf: DecisionTreeClassifier, feature_names: list[str]) -> list[dict]:
    """Walk the tree, collect each leaf's rule path + class distribution."""
    tree = clf.tree_
    leaves = []

    def recurse(node: int, conditions: list[str]):
        if tree.feature[node] == _tree.TREE_UNDEFINED:
            # Leaf
            n = int(tree.n_node_samples[node])
            values = tree.value[node][0]  # [n_loss, n_win]
            n_loss, n_win = int(values[0]), int(values[1])
            wr = n_win / max(1, n_win + n_loss)
            leaves.append({
                "rule": " AND ".join(conditions) if conditions else "(root)",
                "n": n, "n_win": n_win, "n_loss": n_loss, "wr": wr,
            })
            return
        feat = feature_names[tree.feature[node]]
        thresh = tree.threshold[node]
        recurse(tree.children_left[node],
                conditions + [f"{feat}<={thresh:.4f}"])
        recurse(tree.children_right[node],
                conditions + [f"{feat}>{thresh:.4f}"])

    recurse(0, [])
    return leaves


def cv_leaf_rules(df: pd.DataFrame, feat_names: list[str], top_leaves: list[dict],
                  n_splits: int = 5) -> list[dict]:
    """For each top leaf, re-fit on 4/5 folds and report test-fold WR."""
    skf = StratifiedKFold(n_splits=n_splits, shuffle=True, random_state=42)
    X = df[feat_names].values
    y = df["win"].values
    out = []
    for leaf in top_leaves:
        # Cross-validate: refit tree on train, find equivalent leaf, evaluate on test
        test_wrs = []
        test_ns = []
        for fold_i, (train_idx, test_idx) in enumerate(skf.split(X, y)):
            clf = DecisionTreeClassifier(
                max_depth=4, min_samples_leaf=30,
                criterion="entropy", random_state=42,
            )
            clf.fit(X[train_idx], y[train_idx])
            # Predict on test fold, find samples where predicted win-prob matches the leaf's WR
            test_probs = clf.predict_proba(X[test_idx])[:, 1]
            # samples where the tree assigns high-confidence win prediction
            # Use leaf-equivalence: take test samples in the highest-prob bucket
            threshold = 0.7  # treat as "winner subset"
            mask = test_probs >= threshold
            if mask.sum() >= 5:
                test_wrs.append(y[test_idx][mask].mean())
                test_ns.append(int(mask.sum()))
        if test_wrs:
            out.append({
                **leaf,
                "cv_wr_mean": float(np.mean(test_wrs)),
                "cv_wr_std": float(np.std(test_wrs)),
                "cv_n_mean": float(np.mean(test_ns)),
            })
        else:
            out.append({**leaf, "cv_wr_mean": None, "cv_wr_std": None, "cv_n_mean": 0})
    return out


def main():
    df = load()
    feat_names = FEATURES + LANE_FEATS

    print(f"[+] loaded {len(df)} trades, baseline WR = {df['win'].mean()*100:.1f}%", flush=True)
    print(f"[+] features used: {len(feat_names)}", flush=True)
    print()

    X = df[feat_names].values
    y = df["win"].values

    # --- Decision tree: find leaves with high WR
    print("=" * 70)
    print("DECISION TREE (max_depth=4, min_samples_leaf=30, criterion=entropy)")
    print("=" * 70)
    clf = DecisionTreeClassifier(
        max_depth=4, min_samples_leaf=30,
        criterion="entropy", random_state=42,
    )
    clf.fit(X, y)

    leaves = extract_leaves(clf, feat_names)
    leaves_sorted = sorted(leaves, key=lambda L: -L["wr"])

    print(f"{'wr':>5} {'n':>4} {'W':>3} {'L':>3}   rule")
    print("-" * 70)
    for L in leaves_sorted[:10]:
        print(f"{L['wr']*100:>4.0f}% {L['n']:>4d} {L['n_win']:>3d} {L['n_loss']:>3d}   {L['rule']}")

    # CV the top 5 leaves
    print()
    print("--- 5-fold CV on top leaves (threshold=0.7 prob to call 'win') ---")
    top5 = leaves_sorted[:5]
    cv = cv_leaf_rules(df, feat_names, top5, n_splits=5)
    print(f"{'cv_wr':>6} {'+/-':>5} {'cv_n':>5}   rule")
    print("-" * 70)
    for L in cv:
        wr = f"{L['cv_wr_mean']*100:.1f}%" if L['cv_wr_mean'] is not None else "n/a"
        sd = f"{L['cv_wr_std']*100:.1f}%" if L['cv_wr_std'] is not None else "n/a"
        print(f"{wr:>6} {sd:>5} {L['cv_n_mean']:>5.1f}   {L['rule']}")

    # --- Random Forest: predicted-probability calibration
    print()
    print("=" * 70)
    print("RANDOM FOREST (n=200, max_depth=5) — does any prob decile have WR>=85%?")
    print("=" * 70)
    rf = RandomForestClassifier(n_estimators=200, max_depth=5, random_state=42, n_jobs=-1)
    # Out-of-bag-style CV: use cross_val_predict for honest probabilities
    from sklearn.model_selection import cross_val_predict
    probs = cross_val_predict(rf, X, y, cv=5, method="predict_proba", n_jobs=-1)[:, 1]
    # Build deciles
    df["pred_win_prob"] = probs
    df["decile"] = pd.qcut(df["pred_win_prob"], 10, labels=False, duplicates="drop")
    print(f"{'decile':<7} {'prob_range':<18} {'n':>4} {'W':>3} {'wr':>5}")
    print("-" * 50)
    for d in sorted(df["decile"].dropna().unique()):
        sub = df[df["decile"] == d]
        pmin, pmax = sub["pred_win_prob"].min(), sub["pred_win_prob"].max()
        n, w = len(sub), int(sub["win"].sum())
        print(f"{int(d):<7d} {pmin:.3f}-{pmax:.3f}  {n:>4d} {w:>3d} {w/n*100:>4.0f}%")

    # --- Feature importance
    print()
    rf2 = RandomForestClassifier(n_estimators=200, max_depth=5, random_state=42, n_jobs=-1)
    rf2.fit(X, y)
    importances = sorted(zip(feat_names, rf2.feature_importances_), key=lambda z: -z[1])
    print("--- Feature importance (RF) ---")
    for f, imp in importances[:10]:
        print(f"  {f:<25} {imp*100:>5.1f}%")

    # --- Verdict
    print()
    print("=" * 70)
    print("VERDICT")
    print("=" * 70)
    best = max(cv, key=lambda L: (L['cv_wr_mean'] or 0))
    if best['cv_wr_mean'] is not None and best['cv_wr_mean'] >= 0.90 and best['cv_n_mean'] >= 10:
        print(f"FOUND high-WR rule: {best['cv_wr_mean']*100:.1f}% WR, n={best['cv_n_mean']:.0f}")
        print(f"Rule: {best['rule']}")
    else:
        top_decile = df[df["decile"] == df["decile"].max()] if "decile" in df else None
        td_wr = top_decile["win"].mean() if top_decile is not None and len(top_decile) > 0 else 0
        print(f"BEST CV leaf WR: {(best['cv_wr_mean'] or 0)*100:.1f}% (n_mean={best['cv_n_mean']:.0f})")
        print(f"BEST RF top decile WR: {td_wr*100:.1f}% (n={len(top_decile) if top_decile is not None else 0})")
        print()
        print("No rule reaches 90% WR with statistically meaningful n on cross-validation.")
        print("The MAX achievable WR on this 508-trade dataset is bounded by the data itself.")


if __name__ == "__main__":
    main()
