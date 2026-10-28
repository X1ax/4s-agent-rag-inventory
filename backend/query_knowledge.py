"""Read-only retrieval CLI; does not generate answers or execute inventory operations."""
import argparse
import json

from backend.app.config import get_settings
from backend.app.knowledge import CORPORA, KnowledgeError
from backend.app.retrieval import open_index


def main():
    parser = argparse.ArgumentParser(description="查询受控知识片段，不生成答案或执行业务")
    parser.add_argument("question")
    parser.add_argument("--knowledge-base", choices=CORPORA, default="inventory_policies")
    parser.add_argument("--scope", choices=["released_only", "draft_preview"], default="released_only")
    parser.add_argument("--mode", choices=["dense", "hybrid"], default="dense")
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--document-id", action="append")
    parser.add_argument("--document-version", help="与唯一一个--document-id一起使用")
    args = parser.parse_args()
    if args.document_version and (not args.document_id or len(args.document_id) != 1):
        parser.error("--document-version需同时指定唯一一个--document-id")
    versions = {args.document_id[0]: args.document_version} if args.document_version else None
    try:
        with open_index(get_settings()) as index:
            result = index.search(args.question, args.knowledge_base, scope=args.scope, mode=args.mode,
                top_k=args.top_k, document_ids=args.document_id, document_versions=versions)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except KnowledgeError as error:
        print(json.dumps({"status": "failed", "error_code": error.code, "detail": error.detail}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
