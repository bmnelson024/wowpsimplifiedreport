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
import json
import os
import tempfile
import traceback

from flask import Flask, request, send_file, jsonify, after_this_request

from build_report import build

app = Flask(__name__)

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
REPORT_API_KEY = os.environ.get("REPORT_API_KEY")  # required in production
MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100MB total -- up to ~4 Orion exports (a few MB each) per request

app.config["MAX_CONTENT_LENGTH"] = MAX_UPLOAD_BYTES


@app.after_request
def add_cors_headers(resp):
    resp.headers["Access-Control-Allow-Origin"] = ALLOWED_ORIGIN
    resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Report-Key"
    resp.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS, GET"
    # Let browser JS read the warnings header from the fetch() response.
    resp.headers["Access-Control-Expose-Headers"] = "X-Report-Warnings, X-Report-Client"
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
        if supplied != REPORT_API_KEY:
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

    with tempfile.TemporaryDirectory(prefix="wo_report_") as workdir:
        input_pdf = os.path.join(workdir, "input.pdf")
        uploaded.save(input_pdf)

        managed_pdf = None
        if managed_upload:
            managed_pdf = os.path.join(workdir, "managed.pdf")
            managed_upload.save(managed_pdf)
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
                           rmd=rmd)
        except Exception as e:
            app.logger.error("Report generation failed: %s\n%s", e, traceback.format_exc())
            return jsonify({
                "error": "Could not generate the report from this PDF.",
                "detail": str(e),
            }), 422

        client_name = result["data"].get("client_name", "Client")
        warnings = result.get("warnings", [])
        safe_name = "".join(c if c.isalnum() or c in " -_" else "_" for c in client_name).strip() or "Client"
        download_name = f"{safe_name}_Simplified_Review.pdf"

        # Read the file into memory before the temp directory is cleaned up
        # (send_file would otherwise stream from a path that's about to be
        # deleted when this `with` block exits).
        with open(output_pdf, "rb") as f:
            pdf_bytes = f.read()

    import io
    resp = send_file(
        io.BytesIO(pdf_bytes),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=download_name,
    )
    if warnings:
        resp.headers["X-Report-Warnings"] = " | ".join(warnings)
    resp.headers["X-Report-Client"] = client_name
    return resp


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
