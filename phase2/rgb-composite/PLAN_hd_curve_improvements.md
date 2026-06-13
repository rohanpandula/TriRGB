# Implementation Plan — Port NegPy's tone-curve ideas into TriRGB

**Status:** REVISED after Codex review (ready for operator sign-off)
**Author:** Claude
**Date:** 2026-06-01
**Scope target:** `phase2/rgb-composite/rgb_composite/` (Python) + 1 small Swift enum add

### Revision history
- **v2 (post-Codex review):** Codex audited v1 against the code and caught two
  real problems, now fixed in this plan:
  1. **Blocker:** `invert_composite` has an early guard (composite.py:689) that
     raises `NotImplementedError` for any non-`"linear"` id **before**
     `_apply_tone_curve` runs. v1 only patched `_apply_tone_curve` → would never
     reach it. **Fix:** §3.3(b) now updates the guard too (the code comment at
     composite.py:687-688 explicitly anticipates this).
  2. **"No Swift change" was wrong** if operators are to *use* the new look. **Fix:**
     §3.3(a)/§3.4 now include a one-case Swift enum addition; rollout no longer
     silently flips the default `"standard"` look (§5).
  Plus folded in: float64/overflow/pivot-clamp math hardening (§3.1), `LOOK_SETTINGS`
  typing fix (§3.4), and one more already-green test in the audit (§3.5).

---

## 0. Origin & licensing (read first)

These improvements are **ideas/math** observed in **NegPy** (https://github.com/marcinz606/NegPy).
NegPy is **GPL-3 (copyleft)**; TriRGB is **MIT**. Therefore:

- We **do not copy NegPy source**. Not one line.
- We reimplement from **published sensitometry math** — the Hurter–Driffield (H&D)
  characteristic curve modelled as a logistic sigmoid is standard, textbook,
  non-copyrightable mathematics (also described as prose+formulas in NegPy's
  `docs/PIPELINE.md`, which documents math, not code).
- The implementation here is **clean-room**: written against the formula below,
  not against NegPy's `logic.py`/`normalization.py`.

If anyone is uneasy about even idea-level provenance, the fallback is identical:
the H&D sigmoid is independently derivable and predates NegPy by ~70 years.

---

## 1. Goal

Give the rendered **positive workprint** a more filmic, controllable tone response
than the current generic `smoothstep` + `gamma`, and (secondary) make all frames
on one roll render consistently.

Two deliverables, independently shippable:

| # | Deliverable | Value | Scope | Risk |
|---|---|---|---|---|
| **D1** | **H&D logistic-sigmoid tone curve** | High | `rgb-composite` + 1 Swift enum case | Low (additive) |
| **D2** | **Roll-consistent bounds** (shared black/white points across a roll) | High for real rolls | touches `batch-composite` + capture layer | Medium |

**This plan fully specifies D1 and ships it first.** D2 is specified at
interface level and gated behind D1 landing.

---

## 2. What we are NOT doing (and why)

- **Per-channel "D-range clip" / percentile bounding** — **already implemented.**
  `auto_positive_from_composite` (composite.py:344-355) already loops
  `for ch in range(3)` and computes independent per-channel `lo`/`hi` from
  `display_black_percentile`/`display_white_percentile` in density space. Adding
  NegPy's symmetric clip on top would be a second overlapping knob. Skip.
- **White/black post-analysis offset trims** — marginal for an automated pipeline
  that re-derives bounds each render. Skip.
- **Sampled-neutral WB fine-trim** (log-ratio math) — needs a picker UI surface;
  it's a separate feature, and the base is already neutralized by hardware
  calibration. Defer.
- **E-6 / slide mode** — new film type; roadmap, not now.
- **GPU/Metal preview** — only relevant if we add interactive slider editing in
  the Swift app. Not now.

---

## 3. Deliverable 1 — H&D logistic-sigmoid tone curve

### 3.1 The math (clean-room formulation)

Operate on the **already-normalized** positive value `x ∈ [0,1]` (the output of the
existing per-channel density percentile stretch). Produce tone-mapped `y ∈ [0,1]`.

**Core S-curve (contrast `k`, pivot `x0`):**

```
raw(x) = 1 / (1 + exp(-k * (x - x0)))
```

**Endpoint-normalize** so the curve pins 0→0 and 1→1 exactly (no washed blacks /
clipped whites, and preserves the existing endpoint test contract):

```
y(x) = (raw(x) - raw(0)) / (raw(1) - raw(0))
```

- `k` (contrast grade): higher = steeper midtone contrast. `k → 0` ≈ linear.
- `x0` (pivot): tonal center the contrast pivots around (default 0.5).

**Optional toe/shoulder (v1.1, behind params, default off):** gentle asymmetric
softening of deep shadows (toe) and highlights (shoulder) via a monotonic
power-blend on the sub-pivot / super-pivot segments. **v1 ships contrast+pivot
only** to keep the surface small and the monotonicity proof trivial. Toe/shoulder
is a follow-up once v1 is validated on real frames.

**Hard invariants (enforced by tests):**
1. **Monotonic non-decreasing** on [0,1] (no solarization / local-contrast inversion).
2. `f(0) == 0.0` and `f(1) == 1.0` (endpoints pinned).
3. Output ∈ [0,1] for all input ∈ [0,1].
4. **Deterministic** (no RNG) — NFR-11 / SC-2.
5. **Pure per-channel numeric** — no color-vision/by-eye path (NFR-11 / SC-5).
6. Reduces to ≈identity as `k → 0` (graceful degenerate case).

**Numerical hardening (REQUIRED — flagged by Codex review):**
- Compute internally in **float64**, cast back to float32 at the end. float32
  cancellation makes `raw(1) - raw(0)` unstable for small `k`.
- **Near-zero contrast guard is mandatory:** `if contrast <= 1e-4: return x` — at
  exactly `k=0` the normalization denominator is `0` (0/0). This is correctness,
  not just an optimization.
- **Clamp `pivot` to `[0.0, 1.0]`** before use — a pivot far outside the range
  collapses the `raw(1)-raw(0)` denominator.
- **Avoid `exp` overflow:** use a numerically-stable sigmoid (branch on sign of the
  exponent, or `np.clip` the exponent to e.g. ±60 before `np.exp`). Large `k` with
  float32 otherwise emits overflow warnings (values are still correct limits 0/1,
  but we don't want warning spam / NaN risk).
- Add an epsilon floor on the normalization denominator as a final belt-and-braces.

### 3.2 New function (composite.py)

```python
def hd_sigmoid_tone(x: np.ndarray, *, contrast: float, pivot: float = 0.5) -> np.ndarray:
    """Filmic H&D tone curve: endpoint-normalized logistic sigmoid.

    x: float32 array in [0,1]. Returns float32 in [0,1], same shape.
    Monotonic, pins f(0)=0 and f(1)=1. contrast<=~1e-6 returns x unchanged.
    Clean-room implementation of the standard H&D characteristic curve.
    """
```

- Pure NumPy, vectorized (no Python pixel loop — matches the pipeline's
  vectorization rule in 11-RESEARCH.md).
- Guard `contrast` near 0 → return `x` (identity) to avoid 0/0 in normalization.
- float32 throughout; final caller handles uint16 encode.

### 3.3 Wiring — two call sites, both additive

**(a) Workprint path (primary user-visible win)** — `triplet_positive.py`:

- Extend `LOOK_SETTINGS` entries with an optional curve spec, e.g.
  `"curve_type": "sigmoid" | "smoothstep"`, `"contrast"`, `"pivot"`.
- `apply_render_look(...)` gains a `curve_type` branch:
  - `"smoothstep"` → **existing** behavior, unchanged (keeps the legacy look + the
    `test_render_look_adds_midtones_without_moving_endpoints` contract green).
  - `"sigmoid"` → calls `hd_sigmoid_tone(work, contrast=..., pivot=...)`.
- **Look preset changes (conservative — revised after Codex review):**
  - `"flat"`, `"standard"`, `"punchy"` → **all unchanged for now.** We do **not**
    silently change the default operator output. The existing looks keep their
    current smoothstep behavior.
  - **add `"filmic"`** → new opt-in sigmoid look (contrast+pivot). This is the
    operator-visible improvement, selected explicitly.
  - **Fast-follow (separate change, after real-frame A/B):** once `"filmic"` is
    validated on real scans, flip `"standard"` to the sigmoid so it becomes the
    default. Deferring that flip keeps this change a pure *addition* — nothing an
    operator currently relies on shifts underfoot.

- **Swift (one small enum addition — NOT zero, Codex corrected this):** to let the
  operator pick the new look in the app, add `case filmic` to `PositiveRenderLook`
  (PositiveInversionView.swift:215) and a `"Filmic"` entry in its `label` switch
  (~:222). The SwiftUI picker iterates `CaseIterable.allCases`, so it picks up the
  new case automatically; the CLI call already forwards `--look <rawValue>`
  (PositiveInversionView.swift:368). No other Swift change. If we decide to keep
  `"filmic"` CLI-only for the first cut, this Swift edit can be skipped — but then
  operators can't reach it from the app, which defeats the "improve my app" goal.

**(b) Archival path (uses the prepared extension point)** — `composite.py`:

⚠️ **TWO edits required, not one** (Codex caught this). `invert_composite` rejects
non-`"linear"` ids at an **early guard (composite.py:689)** *before* it ever calls
`_apply_tone_curve` (composite.py:719). Patching only `_apply_tone_curve` would be
dead code. The early guard's own comment (composite.py:687-688) says: *"When a
future phase adds a new curve, remove this early check alongside the matching elif in
_apply_tone_curve."* So:

- **Edit 1 — the early guard (composite.py:689):** change from
  `if params.tone_curve_id != "linear":` to allow the known set, e.g.
  `if params.tone_curve_id not in ("linear", "filmic"):` → still raises
  `NotImplementedError` for unknowns.
- **Edit 2 — `_apply_tone_curve` (composite.py:573):** built to grow `elif` branches. Add:
  - `tone_curve_id == "linear"` → identity (**unchanged** — `test_linear_tone_curve_is_identity` stays green).
  - `tone_curve_id == "filmic"` → `hd_sigmoid_tone` using `tone_curve_params`
    interpreted as `(contrast, pivot)`.
  - anything else → `NotImplementedError`.
- **`test_invert_composite_unknown_tone_curve_raises` stays green:** it asserts
  `"s-curve"` raises, and `"s-curve"` is still not in the allowed set. Verified the
  test only matches the string `"s-curve"` in the exception message.
- `invert_composite` keeps `"linear"` as its default/contract; `"filmic"` is opt-in
  via `InversionParams.tone_curve_id`. The "feed-NLP linear archival" philosophy is
  preserved by default; we only *enable* an in-app filmic option for callers who
  want a finished look without NLP.
- **Note:** `InversionParams` already carries `tone_curve_id`, `tone_curve_params:
  tuple[float,...]`, and `gamma` (contracts.py:325-327) — no contract/schema change
  needed. We interpret `tone_curve_params=(contrast, pivot)` for `"filmic"`.

### 3.4 Files touched (D1)

| File | Change |
|---|---|
| `rgb_composite/composite.py` | + `hd_sigmoid_tone()`; **update early guard at :689** to allow `"filmic"`; extend `_apply_tone_curve` with `"filmic"` branch |
| `rgb_composite/triplet_positive.py` | extend `LOOK_SETTINGS` (+ fix its type annotation, see below); `apply_render_look` gains `curve_type`; add `"filmic"` look |
| `rgb_composite/__init__.py` | export `hd_sigmoid_tone` (optional) |
| `phase3/.../PositiveInversionView.swift` | add `case filmic` + `"Filmic"` label to `PositiveRenderLook` enum (~:215). Picker auto-updates via `CaseIterable`. |
| `tests/test_tone_curve.py` (new) | invariants 1-6 + numerical-hardening cases |
| `tests/test_triplet_positive.py` | add a `look="filmic"` render test; assert existing looks still produce valid output |
| `tests/test_inversion.py` | add `tone_curve_id="filmic"` round-trip test (polarity preserved, monotonic, deterministic) |

**`LOOK_SETTINGS` typing (Codex catch):** it's currently
`dict[str, dict[str, float]]` (triplet_positive.py:41). Adding `"curve_type":
"sigmoid"` (a `str`) makes that annotation false — change it to a small `TypedDict`
(preferred) or `dict[str, Any]`.

**No contract (`c41-core`) change.** **Swift: one small enum addition** (above) — not
zero, as v1 wrongly claimed.

### 3.5 Backward-compatibility audit (every existing test that could break)

- `test_linear_tone_curve_is_identity` — **safe**: `"linear"` branch untouched.
- `test_invert_composite_unknown_tone_curve_raises` — **safe**: matches `"s-curve"`,
  still unsupported.
- `test_invert_composite_*` (polarity, determinism, dtype, shape, overflow, base
  guards) — **safe**: all use `tone_curve_id="linear"`.
- `test_render_look_adds_midtones_without_moving_endpoints` — **safe**: calls
  `apply_render_look` with default → `"smoothstep"` branch unchanged.
- `test_render_triplet_positive_writes_composite_positive_and_report` — **safe**:
  asserts the **negative composite** pixel values (deterministic from input) and the
  positive **shape/dtype** only — never positive pixel values. Changing the
  `"standard"` look's curve does not touch these assertions.
- `test_render_triplet_positive_accepts_manual_base_region` — asserts
  `positive_meta["look"] == "flat"`; `"flat"` stays smoothstep. **Safe.**
- `test_render_triplet_preview_writes_scaled_png` — uses default `"standard"` via
  `render_triplet_preview`, but asserts only PNG shape/type/path, never pixels.
  **Safe** (Codex flagged this as an omission from v1's audit).

With the revised conservative rollout (no look's behavior changes; `"filmic"` is
purely additive), **all** of the above are safe by construction — the only way these
tests see different pixels is if we flip an existing look, which is now deferred to a
separate, A/B-gated change.

### 3.6 New tests (D1)

1. `hd_sigmoid_tone`: monotonic (diff ≥ 0 on a dense ramp), endpoints exact
   (`f(0)==0`, `f(1)==1`), range ⊆ [0,1], deterministic (two calls bit-equal),
   `contrast≈0` → ≈ identity (allclose to input), higher `contrast` → steeper
   midtone slope than lower.
2. Workprint: render with `look="filmic"` → valid uint16 HxWx3, polarity preserved
   (body brighter than rebate), `positive_meta["look"]=="filmic"`.
3. Archival: `invert_composite` with `tone_curve_id="filmic",
   tone_curve_params=(k, 0.5)` → still polarity-correct, monotone vs the linear
   result around the pivot, deterministic, dtype/shape/clip invariants hold.

### 3.7 Verification

```bash
cd phase2/rgb-composite
pytest -q                      # full suite green (old + new)
pytest -q tests/test_tone_curve.py tests/test_inversion.py tests/test_triplet_positive.py
```

Manual: render a real composite with each look; eyeball `standard` vs `filmic` vs
`flat`. (Optional A/B: dump a side-by-side PNG strip.)

---

## 4. Deliverable 2 — Roll-consistent bounds (specified, gated behind D1)

**Problem:** `auto_positive_from_composite` derives black/white percentile bounds
**per frame, independently**. Two frames of the same scene/roll can therefore render
at different brightness/contrast. NegPy solves this with roll-average + lockable
bounds (`use_roll_average`, `lock_bounds`, `locked_floors/ceils`).

**Proposed interface (no implementation in this plan):**

- Add an optional `bounds_override: tuple[tuple[float,float], ...] | None` arg to
  `auto_positive_from_composite` (per-channel `(lo, hi)` in density space). When
  provided, skip per-frame percentile analysis and use these.
- New helper in `batch-composite`: analyze a representative subset (or all) of a
  roll's composites, aggregate per-channel `lo`/`hi` (median across frames), and
  pass the locked bounds to each frame's render.
- Operator control: per-roll "lock tone" toggle (Swift app), default off
  (= current per-frame behavior).

**Why gated:** touches the batch layer and capture/roll metadata; larger blast
radius; should land only after D1's curve is validated, so we're not changing two
variables at once.

---

## 5. Rollout order

1. **D1.1** — `hd_sigmoid_tone` + unit tests (pure function, zero integration risk).
   Includes the numerical-hardening cases (§3.1).
2. **D1.2** — `composite.py`: update early guard (:689) **and** add the
   `_apply_tone_curve` `"filmic"` branch (both edits — §3.3b) + `invert_composite`
   `"filmic"` test.
3. **D1.3** — `triplet_positive.py`: add `"filmic"` look + `apply_render_look`
   `curve_type` branch + `LOOK_SETTINGS` type fix; render test. **Existing looks
   untouched.**
4. **D1.4** — Swift: add `case filmic` + label so the app can select it.
5. Run full `pytest` (Python) + `swift build`/`swift test`; manual A/B of `filmic`
   vs `standard`/`flat` on a real frame; commit.
6. **(Fast-follow, separate commit, A/B-gated)** flip `"standard"` → sigmoid as the
   new default once `filmic` looks right on real scans.
7. **(Later, separate change)** D2 — roll-consistent bounds.

## 6. Decisions (was "open questions" — resolved via Codex review)

- **Flip the default `"standard"`?** → **No, not in this change.** Add `"filmic"`
  opt-in first; flip `"standard"` only after real-frame A/B (step 6). Keeps this a
  pure addition with no silent output change. *(Was the main open question; Codex
  recommended the conservative path.)*
- **toe/shoulder?** → **Deferred.** v1 ships contrast+pivot only (trivial
  monotonicity proof). toe/shoulder is v1.1 once the base curve is validated.
- **Default `contrast`/`pivot` for `"filmic"`** → tuned on real frames during D1.3;
  start near `contrast≈4–6`, `pivot=0.5` and adjust by eye on a known-good scan.
- **Keep `"filmic"` CLI-only instead of touching Swift?** → No — the goal is to
  improve the *app*, so the one-line Swift enum add (D1.4) is in scope.
