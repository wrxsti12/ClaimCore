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


def parse_invoice_from_image(gs_path: str) -> dict:
    """從圖片或 PDF 抽取發票資訊"""
    lower = gs_path.lower()

    # 如果是 PDF，走文字抽取
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
    解析發票 PDF
    
    Request Body:
    {
        "invoice_pdf_path": "gs://claimcore/invoice.pdf"
    }
    
    Response:
    {
        "source": "gs://claimcore/invoice.pdf",
        "parsed_fields": {
            "invoice_number": null,
            "invoice_date": null,
            "vendor_name": null,
            "total_amount": null,
            "currency": null,
            "raw_text": "發票完整文字內容..."
        }
    }
    """
    body = request.get_json(force=True)
    gs_path = body.get("invoice_pdf_path")
    
    if not gs_path:
        return jsonify({"error": "invoice_pdf_path is required"}), 400

    try:
        invoice = parse_invoice_from_image(gs_path)

        parsed_fields = {
            "invoice_number": None,
            "invoice_date": None,
            "vendor_name": None,
            "total_amount": None,
            "currency": None,
            "raw_text": invoice.get("raw_text"),
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
    
    Request Body:
    {
        "task": "Open https://example.com and do something..."
    }
    
    Response:
    {
        "success": true,
        "task_id": "xxx-xxx-xxx",
        "status": "completed",
        "output": "任務執行結果描述",
        "result": "完整的執行細節..."
    }
    """
    body = request.get_json(force=True)
    task_description = body.get("task")
    
    if not task_description:
        return jsonify({"error": "task is required"}), 400
    
    api_key = os.environ.get("BROWSER_USE_API_KEY")
    if not api_key:
        return jsonify({"error": "BROWSER_USE_API_KEY not set"}), 500
    
    try:
        # 初始化 Browser Use client
        client = BrowserUse(api_key=api_key)
        
        # 建立並執行任務
        task = client.tasks.create_task(
            task=task_description,
            llm="browser-use-llm"
        )
        
        # 等待任務完成
        result = task.complete()
        
        # 回傳結果
        return jsonify({
            "success": True,
            "task_id": getattr(result, 'id', None),
            "status": "completed",
            "output": getattr(result, 'output', str(result)),
            "result": str(result)
        })
    except Exception as e:
        return jsonify({
            "success": False,
            "error": str(e),
            "error_type": type(e).__name__
        }), 500


@app.route("/run-workflow", methods=["POST"])
def run_workflow():
    """
    執行完整的 workflow（保留原有功能）
    
    Request Body:
    {
        "workflow": "workflow_name",
        "task": {
            "invoice_image_path": "gs://..."
        }
    }
    """
    body = request.get_json(force=True)
    workflow_name = body.get("workflow")
    task = body.get("task", {})

    if not workflow_name:
        return jsonify({"error": "workflow is required"}), 400

    try:
        wf = load_workflow(workflow_name)
        context = {"task": task}

        # 簡化版：只處理 parse_invoice_input
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
