"""Explicit offline index build; no business database writes or chat-model calls."""
import json

from backend.app.config import get_settings
from backend.app.knowledge import KnowledgeError
from backend.app.retrieval import build_index


def main():
    try:
        result = build_index(get_settings(), progress=lambda value: print(json.dumps(value, ensure_ascii=False), flush=True))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KnowledgeError as error:
        print(json.dumps({"index_status": "failed", "error_code": error.code, "detail": error.detail}, ensure_ascii=False))
    except OSError:
        print(json.dumps({"index_status": "failed", "error_code": "index_io_error", "detail": "知识索引目录不可读写"}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
