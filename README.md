# Job-Search Pipeline

Every run: search **JobsPipe** → deterministic **hard filters** → **Claude** match on new jobs
only → append to a **Google Sheet**. A local store (`data/processed_jobs.json`) remembers every
job ever scored so nothing is paid for twice.

## Run locally

```bash
.venv/Scripts/python.exe -m jobpipe run              # full pipeline
.venv/Scripts/python.exe -m jobpipe run --check-config   # validate settings, print them
.venv/Scripts/python.exe -m jobpipe run --dry-run        # every stage, no sheet write
.venv/Scripts/python.exe -m jobpipe fetch --limit 10     # just show what JobsPipe returns
.venv/Scripts/python.exe -m pytest                       # tests
```

Needs `.env` (keys), `config.yaml` (settings), `secrets/sheet-credentials.json` (Google service
account), and a resume at the path in `config.yaml` (`data/resume.docx`).

## Config

Everything tunable lives in `config.yaml` (committed). Key knobs:

| Setting | Meaning |
|---|---|
| `jobspipe.per_query_limit` | max results = **credits** per run |
| `jobspipe.fetch_posted_within_days` | how far back to search |
| `roles.*` | title search stems (JobsPipe matches them as substrings) |
| `hard_filters.seniority.*` | years-of-experience window |
| `output.dedup.rescore_after_days` | re-score a stored job once it's older than this |

Secrets go in `.env` (`JOBSPIPE_API_KEY`, `ANTHROPIC_API_KEY`, `GOOGLE_SHEET_ID`), never in `config.yaml`.

## Run on a schedule with GitHub Actions

The workflow `.github/workflows/pipeline.yml` runs every 2 days at 06:00 IST (plus a manual
"Run workflow" button). Each run rebuilds `.env`, the Google credentials, and the resume from
**GitHub Secrets**, runs the pipeline, and commits the updated `data/processed_jobs.json` back.

### One-time setup

**1. Create the two resume secrets locally** (PowerShell, from the project folder):

```powershell
# RESUME_B64 - copies the base64 to your clipboard
[Convert]::ToBase64String([IO.File]::ReadAllBytes("data\resume.docx")) | Set-Clipboard

# RESUME_SHA256 - prints the hash (lowercase)
(Get-FileHash -Algorithm SHA256 "data\resume.docx").Hash.ToLower()
```

**2. Create a private GitHub repo**, then push this project:

```bash
git init
git add .
git commit -m "job-search pipeline"
git branch -M main
git remote add origin https://github.com/<you>/<repo>.git
git push -u origin main
```

(`.gitignore` keeps `.env`, `secrets/sheet-credentials.json`, and `data/*.docx` out.
`config.yaml` and `data/processed_jobs.json` **are** committed.)

**3. Add repo Secrets** — Settings → Secrets and variables → Actions → New repository secret:

| Name | Value |
|---|---|
| `JOBSPIPE_API_KEY` | from your `.env` |
| `ANTHROPIC_API_KEY` | from your `.env` |
| `GOOGLE_SHEET_ID` | from your `.env` |
| `GOOGLE_SA_JSON` | entire contents of `secrets/sheet-credentials.json` |
| `RESUME_B64` | clipboard from step 1 |
| `RESUME_SHA256` | hash from step 1 |

**4. Test it** — Actions tab → "job-search pipeline" → Run workflow. Check the log. After that the
cron takes over.

### Changing credits later

Edit `jobspipe.per_query_limit` in `config.yaml`, commit, push. The next scheduled run uses it.

### Updating your resume later

Regenerate `RESUME_B64` and `RESUME_SHA256` (step 1) and update both secrets. The pipeline will
notice the change (resume hash) and re-score everything not yet written to the sheet.
