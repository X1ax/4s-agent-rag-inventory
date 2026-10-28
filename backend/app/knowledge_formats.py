"""Controlled native profiles for the registered Office/PDF policy drafts."""
import re
from io import BytesIO

from backend.app.knowledge import CLAUSE, KnowledgeError

METADATA_KEYS = ["文档编号", "版本", "发布状态", "起草日期", "归口岗位", "批准与生效"]
PDF_TABLE_HEADERS = {
    "INV-RCV-004": ["核验项目", "放行条件", "不满足时处理"],
    "INV-WHS-007": ["状态", "实物要求", "库存与放行要求"],
}


def review(code, detail):
    raise KnowledgeError(code, detail, state="needs_review")


def native_metadata(document, title, rows):
    if len(rows) != 6 or any(len(row) != 2 for row in rows) or [row[0] for row in rows] != METADATA_KEYS:
        review("native_metadata", "原生资料元数据表结构不符合登记格式")
    values = dict(rows)
    compact = lambda text: re.sub(r"\s+", "", text)
    if (title != document["title"] or values["文档编号"] != document["document_id"]
            or values["版本"] != document["document_version"]
            or values["起草日期"] != document["drafted_on"] or values["归口岗位"] != document["owner_role"]):
        review("metadata_mismatch", "原生资料身份、版本或归口信息与清单不一致")
    # Only this registered draft profile is supported, not an invented approval parser.
    if (compact(values["发布状态"]) != "待企业核准尚未生效"
            or compact(values["批准与生效"]) != "批准人待登记生效日期待核准"
            or document["source_status"] != "pending_approval" or document["effective_from"] is not None):
        review("native_approval_profile", "原生资料批准状态需人工复核，不能由解析推定生效")
    return {"document_id": values["文档编号"], "version": values["版本"], "title": title,
        "approval_status": "pending_approval", "effective_from": None}


def markdown_table(rows):
    if (len(rows) < 2 or len(rows[0]) < 2 or any(len(row) != len(rows[0]) for row in rows)
            or any(not isinstance(cell, str) or not cell.strip() or "|" in cell or "\n" in cell for row in rows for cell in row)):
        review("native_table", "原生表格行列、空单元格或复杂内容需复核")
    rendered = ["| " + " | ".join(row) + " |" for row in rows]
    rendered.insert(1, "| " + " | ".join(["---"] * len(rows[0])) + " |")
    return rendered


def extract_docx(document, raw):
    from docx import Document
    from docx.text.paragraph import Paragraph

    try:
        doc = Document(BytesIO(raw))
    except Exception:
        review("invalid_docx", "DOCX文件损坏或不是受支持的原生文档")
    if (doc.element.body.xpath(".//w:ins | .//w:del | .//w:moveFrom | .//w:moveTo | .//w:drawing | .//w:object | .//w:txbxContent")
            or any(element.tag.rsplit("}", 1)[-1] not in {"p", "tbl", "sectPr"} for element in doc.element.body)):
        review("unsupported_docx_structure", "DOCX含未处理修订、图形或未支持正文结构，须复核")
    blocks = list(doc.iter_inner_content())
    if len(blocks) < 3 or not isinstance(blocks[0], Paragraph) or blocks[0].style.name != "Title" or isinstance(blocks[1], Paragraph):
        review("native_metadata", "DOCX缺少标题和原生元数据表")
    metadata = native_metadata(document, blocks[0].text, [[c.text for c in row.cells] for row in blocks[1].rows])
    lines, locations, table_ordinal = [], {}, 0
    for number, block in enumerate(blocks, 1):
        if isinstance(block, Paragraph):
            text = block.text.strip()
            if block.style.name == "Title":
                text = "# " + text
            elif block.style.name == "Heading 1":
                if not re.fullmatch(r"\d+ .+", text):
                    review("native_heading", "DOCX章节编号不符合受控格式")
                text = "## " + text
            elif block.style.name.startswith("Heading"):
                review("native_heading", "DOCX深层标题需要复核")
            entries = [text]
            location = {"block_start": number, "block_end": number}
        else:
            table_ordinal += 1
            if block._tbl.xpath(".//w:gridSpan | .//w:vMerge | .//w:tbl"):
                review("complex_docx_table", "DOCX合并或嵌套表格暂不支持，须复核")
            if number == 2:
                continue
            entries = markdown_table([[cell.text.strip() for cell in row.cells] for row in block.rows])
            location = {"block_start": number, "block_end": number, "table_ordinal": table_ordinal}
        for text in entries:
            if "\ufffd" in text or "\x00" in text:
                review("invalid_text", "原生正文含替换乱码或NUL")
            lines.append((len(lines)+1, text))
            locations[lines[-1][0]] = location
    if table_ordinal != 2:
        review("native_table", "DOCX登记的原生业务表格缺失或数量变化，须复核")
    return metadata, lines, locations


def extract_pdf(document, raw):
    from pypdf import PdfReader

    try:
        reader = PdfReader(BytesIO(raw), strict=True)
        if reader.is_encrypted:
            review("encrypted_pdf", "PDF已加密，不能自动解析")
        pages = [page.extract_text() or "" for page in reader.pages]
    except KnowledgeError:
        raise
    except Exception:
        review("invalid_pdf", "PDF文件损坏或正文无法提取")
    if not pages or any(not text.strip() for text in pages):
        review("pdf_no_text", "PDF存在无可用文字层的页面，需复核；本阶段不做OCR")
    lines, locations, pending, metadata = [], {}, None, None
    table_count = 0
    headers = PDF_TABLE_HEADERS.get(document["document_id"])
    if headers is None:
        review("unsupported_pdf_profile", "未登记此PDF的表格结构，须复核")

    def append(text, start, end, **extra):
        lines.append((len(lines)+1, text))
        locations[lines[-1][0]] = {"page_start": start, "page_end": end, **extra}

    def flush():
        nonlocal pending
        if pending:
            append(pending["text"], pending["start"], pending["end"])
            pending = None

    for page_number, text in enumerate(pages, 1):
        raw_lines = [line.strip() for line in text.splitlines() if line.strip()]
        if (len(raw_lines) < 3 or not re.fullmatch(re.escape(document["document_id"]) + r"\s+库存制度受控资料\s+待核准", raw_lines[0])
                or not re.fullmatch(r"待核准稿\s+第\s+" + str(page_number) + r"\s+页", raw_lines[1])):
            review("pdf_page_profile", "PDF页眉页脚不符合登记结构，不能按位置猜测删除")
        # This profile's content stream emits the identified header/footer first.
        body = raw_lines[2:]
        if page_number == 1:
            if len(body) < 14:
                review("native_metadata", "PDF首面缺少受控元数据")
            metadata = native_metadata(document, body[0], [body[i:i+2] for i in range(1, 13, 2)])
            append("# " + body[0], 1, 1)
            body = body[13:]
        cursor = 0
        while cursor < len(body):
            line = body[cursor]
            if body[cursor:cursor+3] == headers:
                flush()
                end = cursor + 3
                while end < len(body) and not CLAUSE.fullmatch(body[end]) and not re.fullmatch(r"\d+ .+", body[end]):
                    end += 1
                cells = body[cursor:end]
                # Only the visually verified, single-line, three-column draft table profile.
                if len(cells) != 15:
                    review("native_table", "PDF表格行列或换行结构变化，须复核")
                rows = [cells[i:i+3] for i in range(0, len(cells), 3)]
                for rendered in markdown_table(rows):
                    append(rendered, page_number, page_number, table_ordinal=2)
                table_count += 1
                cursor = end
                continue
            if re.fullmatch(r"\d+ .+", line):
                flush()
                append("## " + line, page_number, page_number)
            elif CLAUSE.fullmatch(line):
                flush()
                pending = {"text": line, "start": page_number, "end": page_number}
            elif pending:
                pending["text"] += line
                pending["end"] = page_number
            else:
                # Join physical wraps in the document introduction as well as clauses.
                if lines and not lines[-1][1].startswith(("#", "|")):
                    number, previous = lines[-1]
                    lines[-1] = (number, previous + line)
                    locations[number]["page_end"] = page_number
                else:
                    append(line, page_number, page_number)
            cursor += 1
    flush()
    if table_count != 1:
        review("native_table", "PDF应有的原生表格缺失或重复")
    if any("\ufffd" in line or "\x00" in line for _, line in lines):
        review("invalid_text", "PDF正文含替换乱码或NUL")
    return metadata, lines, locations


def native_location(locations, numbers):
    selected = [locations[number] for number in numbers]
    start_key = "page_start" if "page_start" in selected[0] else "block_start"
    end_key = "page_end" if start_key == "page_start" else "block_end"
    return {start_key: min(loc[start_key] for loc in selected), end_key: max(loc[end_key] for loc in selected)}
