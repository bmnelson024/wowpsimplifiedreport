"""
Flask backend for the White Oak Simplified Client Report.

Exposes:
  GET  /health            -- plain liveness check
  POST /generate-report   -- multipart/form-data with an "orion_pdf" file
                              field. Returns the generated 3-page PDF.

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
import os
import tempfile
import traceback

from flask import Flask, request, send_file, jsonify, after_this_request

from build_report import build

app = Flask(__name__)

ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "*")
REPORT_API_KEY = os.environ.get("REPORT_API_KEY")  # required in production
MAX_UPLOAD_BYTES = 40 * 1024 * 1024  # 40MB -- Orion exports are usually a few MB

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

    with tempfile.TemporaryDirectory(prefix="wo_report_") as workdir:
        input_pdf = os.path.join(workdir, "input.pdf")
        uploaded.save(input_pdf)

        output_pdf = os.path.join(workdir, "output.pdf")
        try:
            result = build(input_pdf, output_pdf, workdir)
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
