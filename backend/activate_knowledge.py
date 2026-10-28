"""Activate a validated historical generation only when all current sources still match."""
import argparse
import json

from backend.app.config import get_settings
from backend.app.knowledge import KnowledgeError
from backend.app.retrieval import activate_generation


def main():
    parser = argparse.ArgumentParser(description="校验并恢复历史索引代次，不恢复已撤回资料")
    parser.add_argument("generation_id")
    args = parser.parse_args()
    try:
        result = activate_generation(get_settings(), args.generation_id)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KnowledgeError as error:
        print(json.dumps({"status": "failed", "error_code": error.code, "detail": error.detail}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
