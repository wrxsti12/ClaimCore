from flask import Flask, request, jsonify
import json
import os
from pathlib import Path
from google.cloud import storage
from PIL import Image
from pyzbar.pyzbar import decode as decode_qr
import tempfile
from pypdf import PdfReader
from browser_use_sdk import BrowserUse
import requests
import time

EXCHANGE_RATE_API_KEY = "ec45f233b18cc9fd1a31c2c8"
EXCHANGE_RATE_API_URL = "https://v6.exchangerate-api.com/v6"

app = Flask(__name__)

# ==================== 設定區 ====================
BASE_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = BASE_DIR / "workflow"

# Cloud Run URL (本服務)
CLOUD_RUN_URL = "https://claimcore-384115959752.asia-east1.run.app"

# n8n Webhook URL
N8N_WEBHOOK_URL = "https://pinhua12.app.n8n.cloud/webhook-test/Claimcore"

# ==================== 工具函數 ====================

def load_workflow(name: str) -> dict:
    """載入 workflow JSON 設定檔"""
    path = WORKFLOW_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def download_gcs_file(gs_path: str) -> str:
    """從 GCS 下載檔案到暫存目錄"""
    assert gs_path.startswith("gs://"), "路徑必須以 gs:// 開頭"
    without_scheme = gs_path[5:]
    bucket_name, blob_name = without_scheme.split("/", 1)

    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blob = bucket.blob(blob_name)

    tmp_fd, tmp_path = tempfile.mkstemp(suffix="_invoice")
    os.close(tmp_fd)
    blob.download_to_filename(tmp_path)
    return tmp_path

def parse_invoice_from_pdf(gs_path: str) -> dict:
    """從 PDF 抽取文字"""
    local_path = download_gcs_file(gs_path)
    reader = PdfReader(local_path)
    texts = []
    for page in reader.pages:
        txt = page.extract_text() or ""
        texts.append(txt)

    full_text = "\n".join(texts)

    return {
        "raw_text": full_text,
        "items": [],
        "source": gs_path,
        "note": "PDF text extracted; next step is to parse fields from raw_text."
    }

@app.route("/upload-invoice", methods=["POST"])
def upload_invoice():
    """
    接收前端上傳的檔案，存到 GCS，回傳 gs:// 路徑。
    前端要用 form-data，上傳欄位名稱為 file。
    """
    if "file" not in request.files:
        return jsonify({"error": "no file part"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "empty filename"}), 400

    bucket_name = "claimcore"

    try:
        storage_client = storage.Client()
        bucket = storage_client.bucket(bucket_name)

        blob_name = f"invoices/{int(time.time())}_{file.filename}"
        blob = bucket.blob(blob_name)

        blob.upload_from_file(file.stream, content_type=file.content_type)

        gs_path = f"gs://{bucket_name}/{blob_name}"
        return jsonify({"gs_path": gs_path})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

def parse_invoice_from_image(gs_path: str) -> dict:
    """從圖片或 PDF 抽取發票資訊"""
    lower = gs_path.lower()

    if lower.endswith(".pdf"):
        return parse_invoice_from_pdf(gs_path)

    # 其餘視為圖片，用 QR code 解析
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

# ==================== 匯率工具 ====================

def get_usd_twd_rate_simulated():
    url = f"{EXCHANGE_RATE_API_URL}/{EXCHANGE_RATE_API_KEY}/latest/USD"
    print("DEBUG FX URL:", url)

    resp = requests.get(url, timeout=5)
    print("DEBUG FX STATUS:", resp.status_code)
    print("DEBUG FX BODY:", resp.text[:200])

    data = resp.json()
    rates = data.get("conversion_rates", {})
    twd_rate = rates.get("TWD")
    rate_date_utc = data.get("time_last_update_utc")
    return twd_rate, rate_date_utc

# ==================== API Endpoints ====================

@app.route("/", methods=["GET"])
def home():
    """健康檢查 endpoint"""
    return jsonify({
        "service": "ClaimCore AI 自動報帳系統",
        "version": "1.0.0",
        "status": "running",
        "endpoints": {
            "parse_invoice": f"{CLOUD_RUN_URL}/parse-invoice",
            "run_browser_task": f"{CLOUD_RUN_URL}/run-browser-task",
            "run_workflow": f"{CLOUD_RUN_URL}/run-workflow"
        },
        "n8n_webhook": N8N_WEBHOOK_URL
    })

@app.route("/parse-invoice", methods=["POST"])
def parse_invoice():
    """
    解析發票 PDF；若為 USD 發票則模擬匯率計算折合台幣。
    """
    body = request.get_json(force=True)
    gs_path = body.get("invoice_pdf_path")

    if not gs_path:
        return jsonify({"error": "invoice_pdf_path is required"}), 400

    try:
        invoice = parse_invoice_from_image(gs_path)
        raw = invoice.get("raw_text", "") or ""

        # 粗略欄位解析
        invoice_number = None
        invoice_date = None
        total_amount = None
        currency = None  # 先不預設，後面再判斷

        for line in raw.splitlines():
            if "Invoice\t#:" in line or "Invoice #:" in line:
                invoice_number = line.split(":")[-1].strip()

            if "Invoice\tdate:" in line or "Invoice date:" in line:
                invoice_date = line.split(":")[-1].strip()

            # 金額行：同時處理英文 Total 和中文 總計 / 發票總金額
            if ("Total" in line) or ("總計" in line) or ("發票總金額" in line):
                parts = line.split()

                # 嘗試從這一行判斷幣別
                if "USD" in parts:
                    currency = "USD"
                elif ("TWD" in parts) or ("NT$" in parts) or ("NTD" in parts) or ("幣別:TWD" in line):
                    currency = "TWD"

                # 從右往左找最後一個像數字的 token 當金額
                for token in reversed(parts):
                    try:
                        total_amount = float(token.replace(",", ""))
                        break
                    except ValueError:
                        continue

        # 若仍未判斷出幣別，先預設為 TWD（本幣）
        if currency is None:
            currency = "TWD"

        # 嘗試從全文辨識供應商
        vendor_name = "未知供應商"
        lower_text = raw.lower()
        if "adobe systems software ireland limited" in lower_text:
            vendor_name = "Adobe Systems Software Ireland Limited"
        elif "openai, llc" in lower_text:
            vendor_name = "OpenAI, LLC"

        fx_rate = None
        fx_rate_date = None
        amount_twd = None

        if currency == "USD" and total_amount is not None:
            fx_rate, fx_rate_date = get_usd_twd_rate_simulated()
            if fx_rate is not None:
                amount_twd = total_amount * fx_rate

        parsed_fields = {
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "vendor_name": vendor_name,
            "total_amount": total_amount,
            "currency": currency,
            "fx_rate": fx_rate,
            "fx_rate_date": fx_rate_date,
            "amount_twd": amount_twd,
            "raw_text": raw,
        }

        return jsonify({
            "source": gs_path,
            "parsed_fields": parsed_fields
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/run-browser-task", methods=["POST"])
def run_browser_task():
    """
    執行 Browser Use 自動化任務
    """
    body = request.get_json(force=True)
    task_description = body.get("task")

    if not task_description:
        return jsonify({"error": "task is required"}), 400

    # Debug：確認 Cloud Run 讀到的 API key 前幾碼
    print("DEBUG BROWSER_USE_API_KEY prefix:",
          os.environ.get("BROWSER_USE_API_KEY", "")[:10])

    api_key = os.environ.get("BROWSER_USE_API_KEY")
    if not api_key:
        return jsonify({"error": "BROWSER_USE_API_KEY not set"}), 500

    try:
        client = BrowserUse(api_key=api_key)

        task = client.tasks.create_task(
            task=task_description,
            llm="browser-use-llm",
        )

        result = task.complete()

        return jsonify({
            "success": True,
            "task_id": getattr(result, "id", None),
            "status": "completed",
            "output": getattr(result, "output", str(result)),
            "result": str(result),
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__,
        }), 500


@app.route("/run-workflow", methods=["POST"])
def run_workflow():
    """
    執行完整的 workflow（保留原有功能）
    """
    body = request.get_json(force=True)
    workflow_name = body.get("workflow")
    task = body.get("task", {})

    if not workflow_name:
        return jsonify({"error": "workflow is required"}), 400

    try:
        wf = load_workflow(workflow_name)
        context = {"task": task}

        for step in wf.get("steps", []):
            if step["id"] == "parse_invoice_input":
                gs_path = task.get("invoice_image_path")
                if gs_path:
                    invoice = parse_invoice_from_image(gs_path)
                    context["invoice"] = invoice

        step_ids = [s["id"] for s in wf.get("steps", [])]

        return jsonify({
            "workflow": workflow_name,
            "steps": step_ids,
            "task": task,
            "invoice": context.get("invoice")
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ==================== CORS 支援（前端需要） ====================

@app.after_request
def after_request(response):
    """允許前端跨域請求"""
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
    return response

# ==================== 啟動服務 ====================

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
