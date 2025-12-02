from flask import Flask, request, jsonify
import json
import os
from pathlib import Path

app = Flask(__name__)

BASE_DIR = Path(__file__).resolve().parent
WORKFLOW_DIR = BASE_DIR / "workflow"


def load_workflow(name: str) -> dict:
    path = WORKFLOW_DIR / f"{name}.json"
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


@app.route("/run-workflow", methods=["POST"])
def run_workflow():
    body = request.get_json(force=True)
    workflow_name = body.get("workflow")
    task = body.get("task", {})

    if not workflow_name:
        return jsonify({"error": "workflow is required"}), 400

    # 先只測試：能不能讀到 JSON，並回傳 steps 列表
    wf = load_workflow(workflow_name)
    step_ids = [s["id"] for s in wf.get("steps", [])]

    return jsonify({
        "workflow": workflow_name,
        "steps": step_ids,
        "task": task
    })


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 8080))
    app.run(host="0.0.0.0", port=port)

