"""
Flask backend for the White Oak Simplified Client Report.

Exposes:
  GET  /health            -- plain liveness check
  POST /generate-report   -- multipart/form-data with an "orion_pdf" file
                              field (the full household export). Returns the
                              generated PDF (3 pages, or more when a
                              self-directed split makes page 2 run long).

                              Optional fields (all backward compatible --
                              omit them for a plain household report):
                                ytd_pdf            a second export of the same
                                                   client run for January 1
                                                   through the report's end
                                                   date; its return is shown as
                                                   year to date on page 1
                                managed_pdf        the "all but self-directed"
                                                   export (one file)
                                selfdirected_pdfs  one single-account export
                                                   per self-directed account
                                                   (field may repeat)
                                rmd                JSON list of Required
                                                   Minimum Distribution rows:
                                                   [{"account", "required",
                                                   "taken", "year"?,
                                                   "deadline"?, "as_of"?}]
                              managed_pdf and selfdirected_pdfs must be given
                              together.

Auth: a shared secret passed as the "X-Report-Key" header, checked against
the REPORT_API_KEY environment variable. This is a basic deterrent (keeps
casual/automated traffic off an endpoint that processes real client PDFs)
rather than strong security -- the key lives in the front-end's JS source,
so anyone who reads that source can see it. Good enough for an internal
tool with a small, known set of users; revisit if this ever needs to be
hardened further.

CORS: restricted to the ALLOWED_ORIGIN environment variable (set this to
the Netlify site's URL) so only that page's browser-side fetch() calls are
allowed, in addition to the shared-secret check above.

No review/approval gate: per explicit product decision, this returns the
generated PDF immediately with no human-in-the-loop check. Any uncertainty
the automatic performance-chart detection ran into is surfaced as an
"X-Report-Warnings" response header (informational only -- it does not
block the response) so the front-end can optionally show a heads-up.
"""
import gc
import hmac
import io
import json
import logging
import os
import tempfile
import traceback
import unicodedata

from flask import Flask, request, send_file, jsonify

from build_report import build

app = Flask(__name__)

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
REPORT_API_KEY = os.environ.get("REPORT_API_KEY")  # required in production
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB total -- up to ~4 Orion exports (a few MB each) per request

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES

if not REPORT_API_KEY:
    logging.getLogger(__name__).warning(
        "REPORT_API_KEY is not set: /generate-report accepts requests from anyone. "
        "Set it (and the same value in the Review Tool) on the host.")
if ALLOWED_ORIGIN == "*":
    logging.getLogger(__name__).warning(
        "ALLOWED_ORIGIN is not set: any website's browser code may call this service. "
        "Set it to the Review Tool's Netlify address.")


_PUNCT = {"\u2018": "'", "\u2019": "'", "\u201c": '"', "\u201d": '"', "\u2013": "-", "\u2014": "-", "\u2026": "...", "\u00a0": " "}


def header_safe(text, limit=1800):
    """Make text safe for an HTTP header: ASCII only, one line, bounded length.

    Servers (gunicorn included) refuse headers with characters outside latin-1 and drop the
    connection, which the browser reports as "Failed to fetch" even though the report was built.
    Fund names with curly apostrophes or dashes would otherwise trigger that."""
    text = "".join(_PUNCT.get(ch, ch) for ch in str(text))
    text = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii")
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


@app.errorhandler(413)
def too_large(_e):
    return jsonify({"error": "That upload is too large (limit %d MB in total)." % (MAX_UPLOAD_BYTES // (1024 * 1024))}), 413


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Report-Key"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    # Let browser JS read the warnings / filename headers from the fetch() response.
    resp.headers["Access-Control-Expose-Headers"] = "X-Report-Warnings, X-Report-Client, Content-Disposition"
    return resp


@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


@app.route("/generate-report", methods=["OPTIONS"])
def generate_report_preflight():
    return ("", 204)


@app.route("/generate-report", methods=["POST"])
def generate_report():
    if REPORT_API_KEY:
        supplied = request.headers.get("X-Report-Key", "")
        # Constant-time comparison (bytes, so a non-ASCII header cannot raise).
        if not hmac.compare_digest(supplied.encode("utf-8", "replace"), REPORT_API_KEY.encode("utf-8")):
            return jsonify({"error": "Unauthorized"}), 401

    if "orion_pdf" not in request.files:
        return jsonify({"error": "No file uploaded under field 'orion_pdf'."}), 400

    uploaded = request.files["orion_pdf"]
    if not uploaded.filename:
        return jsonify({"error": "No file selected."}), 400

    # Optional self-directed split: both files together, or neither.
    managed_upload = request.files.get("managed_pdf")
    if managed_upload is not None and not managed_upload.filename:
        managed_upload = None
    sd_uploads = [f for f in request.files.getlist("selfdirected_pdfs") if f and f.filename]
    if bool(managed_upload) != bool(sd_uploads):
        return jsonify({"error": "The self-directed split needs both 'managed_pdf' (the all-but-self-directed "
                                 "export) and at least one 'selfdirected_pdfs' file."}), 400

    # Optional: a second Orion export run for January 1 through the report's end date (year-to-date return).
    ytd_upload = request.files.get("ytd_pdf")
    if ytd_upload is not None and not ytd_upload.filename:
        ytd_upload = None

    # Optional: the name to print on the report instead of Orion's group name
    # (a custom multi-account export is titled e.g. "6 Registrations").
    client_name_override = " ".join((request.form.get("client_name") or "").split())[:120] or None

    # Optional RMD rows (manually entered -- not in the Orion export).
    rmd = None
    rmd_raw = request.form.get("rmd")
    if rmd_raw:
        try:
            rmd = json.loads(rmd_raw)
            if not isinstance(rmd, list):
                raise ValueError("rmd must be a list")
            clean = []
            for r in rmd:
                account = str(r["account"]).strip()
                required, taken = float(r["required"]), float(r.get("taken") or 0)
                if not account or required < 0 or taken < 0:
                    raise ValueError("bad row")
                row = {"account": account, "required": required, "taken": taken}
                if r.get("year"):
                    row["year"] = int(r["year"])
                for k in ("deadline", "as_of"):
                    if r.get(k):
                        row[k] = str(r[k])
                clean.append(row)
            rmd = clean or None
        except (ValueError, KeyError, TypeError):
            return jsonify({"error": "Could not read the RMD rows (each needs an account name and a "
                                     "required amount of zero or more)."}), 400

    # Reject anything that is clearly not a PDF before spending time on it.
    for f in [uploaded, managed_upload, ytd_upload, *sd_uploads]:
        if f is None:
            continue
        head = f.stream.read(1024)
        f.stream.seek(0)
        if b"%PDF-" not in head:
            return jsonify({"error": "'%s' does not look like a PDF file." % (f.filename or "upload")[:80]}), 400

    with tempfile.TemporaryDirectory(prefix="wo_report_") as workdir:
        input_pdf = os.path.join(workdir, "input.pdf")
        uploaded.save(input_pdf)

        managed_pdf = None
        if managed_upload:
            managed_pdf = os.path.join(workdir, "managed.pdf")
            managed_upload.save(managed_pdf)
        ytd_pdf = None
        if ytd_upload:
            ytd_pdf = os.path.join(workdir, "ytd.pdf")
            ytd_upload.save(ytd_pdf)
        selfdirected_pdfs = []
        for i, f in enumerate(sd_uploads):
            p = os.path.join(workdir, f"selfdirected_{i}.pdf")
            f.save(p)
            selfdirected_pdfs.append(p)

        output_pdf = os.path.join(workdir, "output.pdf")
        try:
            result = build(input_pdf, output_pdf, workdir,
                           managed_pdf=managed_pdf,
                           selfdirected_pdfs=selfdirected_pdfs or None,
                           rmd=rmd,
                           client_name_override=client_name_override,
                           ytd_pdf=ytd_pdf)
        except Exception as e:
            app.logger.error("Report generation failed: %s\n%s", e, traceback.format_exc())
            return jsonify({
                "error": "Could not generate the report from this PDF.",
                "detail": str(e),
            }), 422

        client_name = result["data"].get("client_name", "Client")
        warnings = result.get("warnings", [])
        safe_name = "".join(c if c.isalnum() or c in " -_." else "_" for c in client_name.replace("&", "and")).strip() or "Client"
        download_name = f"{safe_name}_Simplified_Review.pdf"

        # Read the file into memory before the temp directory is cleaned up
        # (send_file would otherwise stream from a path that's about to be
        # deleted when this `with` block exits).
        with open(output_pdf, "rb") as f:
            pdf_bytes = f.read()

    gc.collect()
    resp = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )
    if warnings:
        resp.headers["X-Report-Warnings"] = header_safe(" | ".join(warnings))
    # HTTP headers are latin-1 only; a name with other characters must not crash the response.
    resp.headers["X-Report-Client"] = header_safe(client_name, 200) or "Client"
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
