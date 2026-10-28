"""Explicit controlled-source parsing. No .env, AI calls or index publication."""
import argparse
import json

from backend.app.knowledge import KnowledgeError, export_parsed, parse_corpora


def main():
    parser = argparse.ArgumentParser(description="校验并解析登记的MD/TXT/PDF/DOCX知识资料，不调用模型")
    parser.add_argument("--check-only", action="store_true", help="只检查，不导出解析文件")
    args = parser.parse_args()
    try:
        snapshot = parse_corpora()
        result = {"parse_status": snapshot["parse_status"], "parse_id": snapshot["parse_id"],
            "index_status": "not_indexed", **snapshot["summary"]}
        if snapshot["parse_status"] != "parsed":
            result["errors"] = [{k: d[k] for k in ("document_id", "parse_status", "error_code", "detail") if k in d}
                for d in snapshot["documents"] if d["parse_status"] in {"failed", "needs_review"}]
        elif not args.check_only:
            path, created = export_parsed(snapshot)
            result.update(output_directory=str(path), created=created)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if snapshot["parse_status"] == "parsed" else 1
    except KnowledgeError as error:
        print(json.dumps({"parse_status": "failed", "error_code": error.code, "detail": error.detail}, ensure_ascii=False))
        return 1
    except OSError:
        print(json.dumps({"parse_status": "failed", "error_code": "output_unavailable", "detail": "无法导出解析文件，请检查输出目录和权限"}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
