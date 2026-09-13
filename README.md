![10K Brains](10k_brains_logo.png)

# path-nd-uploader

Validates pathology whole-slide images and their metadata before uploading
to Google Cloud Storage. Checks that the slide file isn't corrupted (e.g.
truncated mid-upload) and that its metadata matches the
[Path-ND CDE schema](https://github.com/The-10-000-Brains-Project/pathnd-cdes).
Nothing gets uploaded unless both pass.

## Setup

Requires Python 3.10+ and the `gcloud` CLI.

```bash
python3 -m venv .venv
source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install .

gcloud auth login
gcloud auth application-default login
```

(Both logins are needed — different parts of the tool use different
credential stores.) Run `source .venv/bin/activate` again each new terminal
session.

Check it worked:

```bash
path-nd-uploader schema show
```

## Metadata

Most brain banks already have a spreadsheet — one row per slide. Point
`batch` at it directly (CSV, JSON, or xlsx):

```csv
participant_id,brain_bank_id,study,slide_paths,stain_type
P-00231,BB-4471,Path-ND,slide001.svs,HE
P-00232,BB-4472,Path-ND,slide002.svs,AT8
```

`participant_id`, `brain_bank_id`, `study`, `slide_paths`, `stain_type` are
required. Full field list: `path-nd-uploader schema show`.

If your spreadsheet uses different column names (e.g. BDR's raw export),
pass `--profile bdr` and it's translated automatically — see
`path-nd-uploader batch --help` for available profiles.

There's no per-slide metadata file format — `slide_paths` in the manifest
is what links a row to its file. For a single slide, `--metadata` just
takes a manifest with one row (a one-line CSV works fine).

## Commands

```bash
# Batch: point at a manifest (CSV/JSON/xlsx). Omit --bucket to only validate.
path-nd-uploader batch metadata.csv --bucket my-bucket --report run_report.jsonl

# Or a directory of slide files, with no metadata (integrity-only check)
path-nd-uploader batch ./incoming

# Check or upload a single slide (--metadata is a one-row manifest; omit it
# on validate for an integrity-only check)
path-nd-uploader validate slide001.svs --metadata slide001.csv
path-nd-uploader upload slide001.svs --metadata slide001.csv --bucket my-bucket

# Scan a bucket already uploaded to, for corruption
path-nd-uploader audit my-bucket --prefix Collection_PART/ --report audit_report.jsonl

# Copy already-uploaded slides between buckets (server-side, validates first)
path-nd-uploader transfer source-bucket dest-bucket --prefix Collection_PART/
```

`validate`/`upload` print `[PASS]` or `[FAIL]` with the reason. `batch`,
`audit`, and `transfer` show live progress and write `--report` as
JSON-lines incrementally, so it survives an interrupted run.

`[NEEDS REVIEW]` means something couldn't be auto-resolved but doesn't
block the upload — a human should check it later.

`[COULD NOT VERIFY]` means a network error interrupted the check — it's not
a finding about the file. Re-run to get a real answer.

## Troubleshooting

| You see | Fix |
|---|---|
| `command not found: path-nd-uploader` | `source .venv/bin/activate` |
| `gcloud storage cp failed ... Reauthentication is needed` | `gcloud auth login` |
| Other auth/permission error | `gcloud auth application-default login`, and check bucket access |
| `[error] zero_tail: ...` | The slide file is corrupted/truncated — re-copy the original, don't retry the same file |
| `[error] reconcile.slide_paths: ...` | Metadata's `slide_paths` doesn't match the file being uploaded, or points nowhere |

## Development

```bash
pip install -e ".[dev]"
pytest

scripts/update_schema.sh v1.1.0   # update the pinned CDE schema
```
