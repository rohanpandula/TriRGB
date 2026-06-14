# rgb-composite

Takes three narrowband-RGB RAW exposures of a single frame (one each under red, green, and blue Scanlight illumination) and composites them into a single 16-bit linear ProPhoto-RGB TIFF.

Phase 2 / Deliverable 2B of the film scanner build — see `../../PROJECT.md`.

## What this stage does, and does not, do

| Step | Where it happens |
|---|---|
| Linear demosaic of each RAW | **Here** (`rawpy.postprocess`) |
| Discard cross-talk channels under narrowband illumination | **Here** (take channel 0 from R-lit, ch 1 from G-lit, ch 2 from B-lit) |
| Stack into a 16-bit RGB TIFF | **Here** |
| Negative → positive inversion | **NOT HERE** — downstream in FilmLab or NLP |
| Per-stock WB / black point / curve | **NOT HERE** — downstream |
| Dust / scratch removal | Not in this project at all |

The output is a *positive-numbers* representation of a *negative* image: bright film base → high RGB values; deep blacks in the original scene → low values. This is the input format FilmLab and Negative Lab Pro expect.

## Install

```bash
cd phase2/rgb-composite
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

Dependencies: `rawpy` (libraw bindings), `numpy`, `tifffile`, `imagecodecs`.

## Usage

```bash
rgb-composite \
  --r path/to/Frame001_R.ARW \
  --g path/to/Frame001_G.ARW \
  --b path/to/Frame001_B.ARW \
  --out path/to/Frame001.tif
```

Optional:
- `--no-sidecar` — don't write the `.colorspace.txt` sidecar (the embedded ICC profile + TIFF tags remain).

Exit codes: `0` success, `1` on any failure (including dimension mismatch — meaning the film moved between captures).

## Color profile

Every image output carries an **embedded ICC profile** (`rgb_composite.icc`,
built from the ProPhoto primaries in `dng.py`) so viewers color-manage it
instead of falling back to sRGB:

- **Linear composite** (TIFF) → linear ProPhoto-RGB, D50. The DNG variant
  instead carries full DNG colorimetry tags (`ColorMatrix1`/`ForwardMatrix1`/…).
- **Rendered positive + preview** (`triplet_positive`) → ProPhoto primaries
  with a 2.2 display gamma. 2.2 (not the standard ROMM 1.8) matches the tone the
  untagged preview already showed, so tagging corrects only the previously-wrong
  primaries and leaves tone essentially unchanged. The preview PNG and the
  exported positive share one profile, so on-screen matches the file (WYSIWYG).

## The demosaic parameters (locked)

```python
{
    "gamma":          (1, 1),                     # linear
    "no_auto_bright": True,                       # no auto exposure
    "output_bps":     16,                         # 16-bit per channel
    "use_camera_wb":  False,
    "user_wb":        (1.0, 1.0, 1.0, 1.0),       # unity WB
    "output_color":   rawpy.ColorSpace.ProPhoto,  # wide gamut
}
```

These are mandated by `PROJECT.md`. Test `test_demosaic_kwargs_match_project_md` locks them down — any silent change to e.g. `use_camera_wb=True` produces visually-plausible but incorrect output, so the test exists to catch a refactor regression specifically.

## Why we take only one channel per exposure

Under a 665nm pure-red LED, the Bayer red sites of the sensor receive nearly all the light. The green and blue sites pick up only crosstalk (a combination of sensor IR sensitivity, LED side-lobe emission, and inter-channel optical bleed). The crosstalk values are unrelated to the negative's color content and would only contaminate the composite.

Same logic for the G-lit and B-lit captures:

```
R-lit capture → demosaic → take channel 0 → goes into composite's R channel
G-lit capture → demosaic → take channel 1 → goes into composite's G channel
B-lit capture → demosaic → take channel 2 → goes into composite's B channel
```

This is the central reason narrowband-RGB scanning produces better color than white-light scanning: each channel of the composite came from a sensor pixel that saw only its own color.

## Library usage

```python
from rgb_composite import composite_triplet

out = composite_triplet(
    "Frame001_R.ARW", "Frame001_G.ARW", "Frame001_B.ARW",
    "Frame001.tif",
)
```

The batch wrapper (`phase2/batch-composite`) imports this function directly.

## Tests

```bash
pytest
```

Tests patch `demosaic_linear` to return synthetic 16-bit arrays — no actual RAW files needed. They verify:
- The correct channel is taken from each lit exposure (the central correctness property).
- The output is NOT inverted (a regression that would visually plausible).
- Dimension mismatch raises with a clear error.
- The locked rawpy parameters match PROJECT.md exactly.
- Sidecar, embedded ICC profile, and metadata tags are written.

When real ARW samples become available, add an integration test that runs end-to-end. The unit tests guarantee correctness of everything *except* the rawpy invocation itself.
