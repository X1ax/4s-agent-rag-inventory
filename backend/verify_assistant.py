"""Real HTTP synthetic conversation smoke; uses the already running local server."""
import json
import time
from datetime import UTC, datetime

import httpx
from dotenv import dotenv_values

from backend.app.config import Settings
from backend.app.knowledge import ROOT

QUESTIONS = [
    "你好，介绍一下你现在能帮我做什么。",
    "调拨已发出但目标仓还没收货时，目标仓可以领用这批备件吗？",
    "那收货之后呢？",
    "换个问题，燃油车启动时发动机完全不转，只有咔嗒声，未做检测，没有烟雾或燃油泄漏。蓄电池要直接换吗？",
    "那起动机呢？",
    "联网查找汽车蓄电池检测的官方建议，优先VARTA或博世公开资料，给出网页来源。",
    "帮我创建采购单。",
]


def main():
    settings = Settings()
    local = dotenv_values(ROOT / ".env")
    report = {"purpose": "M3.5 synthetic multi-turn HTTP development smoke; not M3.6 evaluation",
        "created_at": datetime.now(UTC).isoformat(), "model": settings.model_id, "runs": []}
    with httpx.Client(base_url="http://127.0.0.1:8000", timeout=55) as client:
        response = client.post("/api/auth/login", json={"username": "demo_admin", "password": local.get("DEMO_ADMIN_PASSWORD", "")})
        if response.status_code != 200:
            raise SystemExit("本机演示账号登录失败，未运行在线评测")
        client.headers["Authorization"] = "Bearer " + response.json()["access_token"]
        thread = client.post("/api/assistant/threads").json()
        report["thread_id"] = thread["id"]
        from uuid import uuid4
        for question in QUESTIONS:
            started = time.monotonic()
            response = client.post(f"/api/assistant/threads/{thread['id']}/messages", json={"message": question,
                "client_request_id": uuid4().hex, "knowledge_scope": "draft_preview", "allow_web": True})
            row = {"question": question, "http_status": response.status_code, "response": response.json(), "elapsed_seconds": round(time.monotonic()-started, 3)}
            report["runs"].append(row)
            print(response.status_code, row["response"].get("answer", {}).get("answer_status", row["response"].get("error_code")), row["elapsed_seconds"], flush=True)
        history = client.get(f"/api/assistant/threads/{thread['id']}").json()
        report["persisted_turn_count"] = len(history["turns"])
    path = ROOT / "evals/runs" / ("m3-5-conversation-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + ".json")
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print("report", path.relative_to(ROOT), flush=True)


if __name__ == "__main__":
    main()
