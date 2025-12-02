from flask import Flask, request, jsonify
import json
import os
from pathlib import Path
from google.cloud import storage
from PIL import Image
from pyzbar.pyzbar import decode as decode_qr
import tempfile
import os



app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = BASE_DIR / "workflow"


def load_workflow(name: str) -> dict:
    path = WORKFLOW_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
    
def download_gcs_file(gs_path: str) -> str:
    # gs_path 例如: gs://ai-expense-agent-bucket/test.jpg
    assert gs_path.startswith("gs://")
    without_scheme = gs_path[5:]
    bucket_name, blob_name = without_scheme.split("/", 1)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix="_invoice.jpg")
    os.close(tmp_fd)
    blob.download_to_filename(tmp_path)
    return tmp_path


def parse_invoice_from_image(gs_path: str) -> dict:
    # 先依副檔名判斷
    lower = gs_path.lower()
    if lower.endswith(".pdf"):
        # 目前不嘗試用 Pillow 開 PDF，避免 UnidentifiedImageError
        # 先只回傳路徑，之後可以改成呼叫 Gemini 做 PDF 解析
        return {
            "raw_text": None,
            "items": [],
            "note": "PDF file detected; QR decoding is skipped in this version.",
            "source": gs_path
        }

    # 其餘視為圖片，照舊用 Pillow + pyzbar 解 QR
    local_path = download_gcs_file(gs_path)
    img = Image.open(local_path)

    codes = decode_qr(img)
    if not codes:
        return {"raw_text": None, "items": [], "source": gs_path}

    data = codes[0].data.decode("utf-8")
    return {
        "raw_text": data,
        "items": [],
        "source": gs_path
    }




if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

