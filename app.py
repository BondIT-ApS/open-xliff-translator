import os
import re
import xml.etree.ElementTree as ET

import defusedxml.ElementTree as DET
import requests
from flask import Flask, request, send_from_directory, jsonify, render_template
from werkzeug.utils import secure_filename

app = Flask(__name__, template_folder="templates")
UPLOAD_FOLDER = "uploads"
PROCESSED_FOLDER = "processed"
LIBRETRANSLATE_URL = "http://libretranslate:5000/translate"

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(PROCESSED_FOLDER, exist_ok=True)

def fix_placeholder_formatting(text):
    """Ensures placeholders like %1$s and %n remain correctly formatted with a leading space if needed."""
    if text:
        text = re.sub(r"(?<!\s)%\s*(\d+)\s*\$", r" %\1$", text)  # Ensure space before %1$s
        text = re.sub(r"(?<!\s)%\s*n", r" %n", text)  # Ensure space before %n
    return text


def translate_text(text, target_lang="da"):
    """Translates text using LibreTranslate."""
    payload = {"q": text, "source": "auto", "target": target_lang, "format": "text"}
    response = requests.post(LIBRETRANSLATE_URL, json=payload)
    if response.status_code == 200:
        return response.json().get("translatedText", text)
    return text  # Fallback if translation fails

def translate_xliff(input_file, output_file, target_lang="da"):
    """Parses an XLIFF file, translates text, and saves the translated file in the correct Transifex format."""
    tree = DET.parse(input_file)  # Securely parse XML
    root = tree.getroot()

    for trans_unit in root.findall(".//trans-unit"):
        source = trans_unit.find("source")
        target = trans_unit.find("target")

        if source is not None:
            translated_text = translate_text(source.text, target_lang)
            translated_text = fix_placeholder_formatting(translated_text)

            if target is None:
                target = ET.SubElement(trans_unit, "target")  # Ensure Transifex compatibility
            target.text = translated_text
            target.set("state", "needs-review-translation")  # Set state for Transifex validation

    # Convert to standard ElementTree for writing the XML safely
    new_tree = ET.ElementTree(root)
    new_tree.write(output_file, encoding="utf-8", xml_declaration=True)

    return output_file

@app.route("/")
def index():
    return render_template("index.html")

@app.route("/upload", methods=["POST"])
def upload_file():
    if "file" not in request.files:
        return jsonify({"error": "No file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    filename = secure_filename(file.filename)
    file_path = os.path.join(UPLOAD_FOLDER, filename)
    file.save(file_path)

    translated_filename = secure_filename(f"translated_{filename}")
    output_file = os.path.join(PROCESSED_FOLDER, translated_filename)
    translate_xliff(file_path, output_file)

    return jsonify({"message": "File processed successfully", "download_url": f"/download/{translated_filename}"})

@app.route("/download/<filename>", methods=["GET"])
def download_file(filename):
    safe_filename = secure_filename(filename)
    file_path = os.path.join(PROCESSED_FOLDER, safe_filename)

    # Ensure the file exists before attempting to send it
    if not os.path.exists(file_path):
        return jsonify({"error": "File not found"}), 404

    return send_from_directory(PROCESSED_FOLDER, safe_filename, as_attachment=True)

if __name__ == "__main__":
    debug_mode = os.getenv("FLASK_DEBUG", "False").lower() in ["true", "1"]
    app.run(host="0.0.0.0", port=5003, debug=debug_mode)
