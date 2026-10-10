# Offline demo snapshots

These 15 profiles are deployable source inputs for `GC_MODE=demo`. Each numeric
JSON file contains API `data`, selected history under `datasets.buyer` and
`datasets.seller`, its original `fetched_at`, and an `origin` label.

The initial bundle contains **eight existing redacted API captures and seven
dataset-only profiles**, because MERP was unreachable during preparation.
Dataset-only files have empty API data and no API capture date. See `manifest.json`
for the exact source of each profile. They are not fresh API captures.

Profiles without source history for a particular role intentionally use cold start:
buyer GLIDs `2126164`, `2342705`, `2416597`, `2614325`, `2929150`, and seller GLID `7195425`.

Names, company names, products, cities, and conversation content remain for
personalisation. Contact details, credentials, tax IDs, addresses and URLs are
redacted. This is not a fully anonymised dataset. Review these curated files before
publishing; use a private GitHub repository with Render access for the demo.

Refresh all 15 on the IndiaMART network:

```bash
app/.venv/bin/python app/demo_export.py
```

Choose other IDs using `--glids 1834784 624518 ...`. Exporting always uses live
source collection, regardless of the server's `GC_MODE`. Failed captures preserve
previous files and produce a nonzero exit code; check the manifest for failures.
Commit refreshed JSON inputs and redeploy to update the hosted source snapshot.
The hosted service never fetches new MERP activity automatically.
