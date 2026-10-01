# White Oak Simplified Client Report — backend

A small Python web service that wraps the verified `white-oak-simplified-review`
report pipeline (extraction + chart + PDF assembly, ported from the skill)
behind one HTTP endpoint, so the Five Points Review Tool's "Simplified Client
Report" button can call it directly instead of handing you a copy-paste
prompt.

## What this does NOT need

No Anthropic/Claude API key. The pipeline is fully deterministic Python
(pdfplumber for extraction, matplotlib for charts, reportlab for layout) —
Claude was only ever the *environment* that ran this code during
development, not something the report generation calls at runtime.

## One important caveat

The performance-chart page/crop location used to be calibrated by hand per
client (see the original skill's notes). This backend automates that with
a pixel-scanning heuristic (`extract_data.locate_performance_chart`) that's
been sanity-checked against a synthetic test page, but **not yet against a
real Orion export** (none was available in the environment this was built
in). Before trusting this for real client reports, run it once against a
real Orion PDF and open page 3 of the output to confirm the performance
chart crop looks right. If it's off, the fix is either to tune the
navy-color tolerance / fallback margin in `extract_data.py`, or to pass
explicit `perf_page_index` / `perf_crop_box` overrides for that client (the
mechanism from the original skill is preserved in `build()`).

By design (per your instruction), this service returns the generated PDF
immediately with no review gate — any detection uncertainty is only
surfaced as an informational `X-Report-Warnings` response header.

## Deploying (GitHub + Render)

1. **Push this folder to a GitHub repo.**
   ```
   cd simplified-report-backend
   git init
   git add .
   git commit -m "Initial backend"
   gh repo create white-oak-report-backend --private --source=. --push
   ```
   (Or create the repo on github.com first, then `git remote add origin ...`
   and `git push`.)

2. **Create a Render account** at render.com (free to start) if you don't
   have one, using your GitHub login so it can see your repos.

3. **New → Web Service**, select the repo you just pushed.

4. Render should detect the `Dockerfile` automatically ("Environment:
   Docker"). If it asks for a build/start command instead, leave both
   blank — the Dockerfile's `CMD` handles it.

5. **Environment variables** (Render's dashboard → your service →
   Environment):
   - `ALLOWED_ORIGIN` = `https://whiteoak5pointsreview.netlify.app`
     (restricts which website's browser JS can call this API)
   - `REPORT_API_KEY` = any long random string you make up, e.g. run
     `openssl rand -hex 24` locally and paste the result. This is the
     shared secret the front-end will send.

6. Deploy. Render gives you a URL like
   `https://white-oak-report-backend.onrender.com`. Note it — the front-end
   needs it.

7. **Test it's alive**: visit `https://<your-url>/health` in a browser —
   should show `{"status": "ok"}`.

8. **Note on Render's free tier**: a free web service spins down after
   15 minutes of no traffic and takes ~30-60 seconds to wake up on the next
   request. The first report generated after a quiet period will feel slow
   for that reason — not a bug. A paid instance ($7/mo as of when this was
   written) removes that delay if it becomes annoying.

## Adding the White Oak logo

The header logo is optional — the pipeline runs fine without it (it's just
omitted from the page header). To add it: drop a `white_oak_logo.png`
(a clean, palette-optimized PNG, ideally under ~50KB) into this folder's
`static/` directory, commit, and push — Render will redeploy automatically.

## Updating later

Any code change: commit and `git push`. Render auto-redeploys on every push
to the connected branch.

## Local testing (optional, before deploying)

```
pip install -r requirements.txt --break-system-packages
python3 app.py
# in another terminal:
curl -F "orion_pdf=@/path/to/some_orion_export.pdf" http://localhost:5000/generate-report -o test_output.pdf
```
