![10K Brains](10k_brains_logo.png)

# path-nd-uploader

Checks that a pathology whole-slide image is intact — not truncated or
zero-filled — before it's uploaded to Google Cloud Storage, and that its
metadata matches the
[Path-ND CDE schema](https://github.com/The-10-000-Brains-Project/pathnd-cdes).
Nothing uploads unless both pass. It can also scan a bucket that's already
been uploaded to.

## What it checks on a slide

Two tiers. The **fast checks run by default** — a few small reads, no full
download, cheap enough to run against every file in a bucket. The **deep
check is opt-in** (`--deep`) and needs the whole file on local disk.

**Fast checks (default):**

Besides the table below, the structural check also reports a page whose
directory entry is empty or zero-filled (which otherwise parses as a shorter
pyramid), and samples about 64 small windows of tile data, weighted by page size,
failing if any compressed tile window is entirely zero. Sampling is cheap enough
for bucket audits (about +50 range reads per object) but only sees a fraction of
the file, so a small zeroed chunk can slip through; `--deep` is exhaustive.

| Check | What it confirms | Fails when |
|---|---|---|
| **TIFF header** | The file starts with a valid TIFF magic number (classic or BigTIFF, either byte order). TIFF-based formats (`.svs .ndpi .scn .bif .tif .tiff`); `.svslide` is checked for its SQLite header instead. | The file is under 4 bytes, or the first bytes aren't a TIFF magic number. |
| **Zero-tail** | The end of the file isn't a long run of zero bytes. Reads the last 64 MiB and measures the actual trailing zero run. | The trailing zero run is larger than `max(1 MiB, 0.5% of file size)`, or the file is 0 bytes. This is the signature of an interrupted upload — including a file that was zero-padded back to its original length, which passes a size check but fails here. |
| **Structural completeness** | Every TIFF page's tile/strip data fits inside the file's actual size. Parses the TIFF directory over range reads, never the whole file. TIFF-based formats only. | A page claims bytes past the end of the file (physically short / cut-off copy), the page-directory chain points past the end of the file (a cut that leaves the first page intact), the directory can't be parsed, there are no readable pages, or the first page is smaller than 512×512. |

Other supported formats (`.mrxs`, `.vms`, `.vmu`) get the zero-tail check
only; the header and structural checks don't apply to them. Those multi-file
formats also get a **bundle completeness** check on local files: `.mrxs`
needs its sibling data directory, `Index.dat`, `Slidedat.ini`, and every `Data*.dat` it
lists; `.vms`/`.vmu` need every image/optimisation file their index
references.

Supported formats are the ones OpenSlide can open. Known pathology formats it
can't (`.czi`, `.vsi`, `.qptiff`, `.isyntax`, `.dcm`, ...) are rejected with an
explicit "recognized but not yet supported" error rather than half-validated.

**Deep check (`--deep`, local files only, needs OpenSlide):**

Opens the slide with OpenSlide, walks every pyramid level, and reads small
regions at the corners, edges and centre of each level. Fails on an open
error, zero pyramid levels, implausibly small dimensions, or any level that
can't be read.

It then streams through **all compressed tile data** (TIFF-based formats) looking
for zero-filled regions: two or more adjacent all-zero 4 KiB blocks inside
compressed image data is never valid (JPEG/JPEG 2000/LZW output has no such
runs), so any zeroed chunk anywhere in the image data is found, down to
~12 KiB. Reads the whole file (about 3 s for a 7 GB slide on a local SSD).
Uncompressed pages and the gaps between tiles are excluded.

**Known limits:**
- MIRAX tile data (`Data*.dat`) is checked for presence only. A truncated or
  zero-filled data file is not detected, since pristine MIRAX data files
  legitimately end in long zero runs, so a zero-tail heuristic would
  false-positive on good slides.
- Zero-filled regions are detected, but other corruption of tile *content*
  (flipped bits, garbage that isn't zero) is only caught if it breaks the
  directory structure or an OpenSlide read; the tool doesn't decode every tile.
- Without `--deep`, a zeroed run in the middle of a file is found only if one of
  the ~64 sampled tile windows lands in it (larger runs are likelier to be hit).
- OpenSlide 4.0.x can't open some valid variants (Ventana scanners that write
  joint direction `LEFT`; Leica SCNs with several ROIs at different
  resolutions). That is reported as a `warning`, not a failure, since the fast
  checks already passed. For those files the fast checks are the only line of
  defense.
- For `.mrxs`/`.vms`/`.vmu` in a bucket audit, sidecar files are looked up by
  object name under the index object's prefix.

**What it does not check:** image or diagnostic quality — focus, staining,
artifacts, tissue coverage. That's a separate job (e.g. HistoQC). This tool
answers one question: is the file whole and structurally intact?

## Setup

Requires Python 3.10+ and the `gcloud` CLI.

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .

gcloud auth login
gcloud auth application-default login
```

Both logins are needed — different parts of the tool use different
credential stores. Re-run `source .venv/bin/activate` in each new terminal.

## Commands

```bash
# Validate + upload a batch from a manifest (CSV/JSON/xlsx). Omit --bucket to only validate.
path-nd-uploader batch metadata.csv --bucket my-bucket --report run_report.jsonl

# A directory of slides, no metadata — integrity check only
path-nd-uploader batch ./incoming

# A single slide
path-nd-uploader validate slide001.svs --metadata slide001.csv
path-nd-uploader upload slide001.svs --metadata slide001.csv --bucket my-bucket

# Scan an already-uploaded bucket for corruption (add --deep for the pyramid walk)
path-nd-uploader audit my-bucket --prefix Collection_PART/ --report audit_report.jsonl

# Copy slides between buckets, server-side, integrity-checked first
path-nd-uploader transfer source-bucket dest-bucket --prefix Collection_PART/
```

`validate`/`upload` print `[PASS]` or `[FAIL]` with the reason. `batch`,
`audit`, and `transfer` show live progress and write `--report` as JSON-lines
incrementally, so the file survives an interrupted run. `[COULD NOT VERIFY]`
means a network error interrupted the check, not a finding about the file —
re-run it.

## Metadata

Most brain banks already have a spreadsheet, one row per slide. Point `batch`
at it directly (CSV, JSON, or xlsx):

```csv
participant_id,brain_bank_id,study,slide_paths,stain_type
P-00231,BB-4471,Path-ND,slide001.svs,HE
```

`participant_id`, `brain_bank_id`, `study`, `slide_paths`, `stain_type` are
required; `slide_paths` links each row to its file. Full field list:
`path-nd-uploader schema show`. If your export uses different column names,
pass `--profile bdr` (see `path-nd-uploader batch --help` for profiles).

## Development

```bash
pip install -e ".[dev]"
pytest
```
