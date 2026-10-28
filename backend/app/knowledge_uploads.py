"""Employee submissions, administrator review, and a separate approved-source registry."""
import asyncio
import io
import json
import sqlite3
import threading
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4
from contextlib import contextmanager
from zipfile import ZipFile

from chromadb.errors import ChromaError

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import Field

from backend.app.knowledge import CORPORA, KnowledgeError, digest
from backend.app.knowledge_answers import StrictModel
from backend.app.security import administrator, current_user

router = APIRouter(prefix='/api/knowledge/submissions', tags=['知识资料上传与审核'])
REVIEW_LOCK = threading.RLock()  # One local index owner; serialize approvals and manifest publication.


def extract_blocks(raw, suffix):
    if suffix in {'md', 'txt'}:
        try: text = raw.decode('utf-8-sig')
        except UnicodeError: raise HTTPException(422, '文本资料需使用UTF-8编码') from None
        blocks, paragraph, start = [], [], 1
        for i, line in enumerate(text.splitlines()+[''], 1):
            if line.strip():
                if not paragraph: start = i
                paragraph.append(line)
            elif paragraph:
                blocks.append(('\n'.join(paragraph), {'line_start': start, 'line_end': i-1}))
                paragraph = []
    elif suffix == 'pdf':
        from pypdf import PdfReader
        try:
            pdf = PdfReader(io.BytesIO(raw))
            if pdf.is_encrypted or len(pdf.pages) > 100: raise ValueError
            blocks = [(page.extract_text() or '', {'page_start': i, 'page_end': i}) for i, page in enumerate(pdf.pages, 1)]
        except Exception: raise HTTPException(422, 'PDF无法解析、已加密或超过100页') from None
    else:
        from docx import Document
        from docx.oxml.ns import qn
        try:
            with ZipFile(io.BytesIO(raw)) as archive:
                if len(archive.infolist()) > 3000 or sum(x.file_size for x in archive.infolist()) > 25*1024*1024: raise ValueError
                if any('vbaproject' in x.filename.lower() for x in archive.infolist()): raise ValueError
            document = Document(io.BytesIO(raw))
            blocks = []
            for i, element in enumerate(document.element.body, 1):
                if element.tag == qn('w:p'):
                    text = ''.join(element.itertext()) if not element.xpath('.//w:t') else ''.join(n.text or '' for n in element.xpath('.//w:t'))
                elif element.tag == qn('w:tbl'):
                    text = '\n'.join(' | '.join(''.join(n.text or '' for n in cell.xpath('.//w:t')) for cell in row.xpath('./w:tc')) for row in element.xpath('./w:tr'))
                else: continue
                blocks.append((text, {'block_start': i, 'block_end': i, **({'table_ordinal': len([b for b in blocks if 'table_ordinal' in b[1]])+1} if element.tag == qn('w:tbl') else {})}))
        except Exception: raise HTTPException(422, 'DOCX损坏、格式不支持或解压体积过大') from None
    chunks = []
    for text, location in blocks:
        text = text.strip()
        if '\x00' in text or '\ufffd' in text: raise HTTPException(422, '资料包含无效文本字符')
        # Keep paragraphs and table rows together; fail rather than silently split a condition.
        for paragraph in text.split('\n\n'):
            if not paragraph.strip(): continue
            if len(paragraph) > 3200:
                raise HTTPException(422, '单段或单页超过3200字符，请按段落拆分后重新上传')
            chunks.append({'text': paragraph, 'location': location})
    if not chunks: raise HTTPException(422, '未提取到文字；扫描件暂不支持OCR')
    if sum(len(c['text']) for c in chunks) > 200000 or len(chunks) > 300:
        raise HTTPException(422, '资料过长，请按主题拆分后上传')
    return chunks


class SubmissionStore:
    def __init__(self, root):
        self.root = Path(root)
        self.directory = self.root/'data/knowledge/uploads'
        self.directory.mkdir(parents=True, exist_ok=True)
        self.path = self.directory/'submissions.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS submissions (id TEXT PRIMARY KEY,user_id INTEGER,title TEXT,kb TEXT,format TEXT,path TEXT,sha TEXT,chunks TEXT,status TEXT,reviewer_id INTEGER,review_note TEXT,created_at TEXT,reviewed_at TEXT)')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path)
        db.row_factory = sqlite3.Row
        try:
            with db: yield db
        finally: db.close()

    def items(self, user_id, admin=False):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM submissions'+('' if admin else ' WHERE user_id=?')+' ORDER BY created_at DESC LIMIT 200', () if admin else (user_id,)).fetchall()
        return [self.public(dict(r)) for r in rows]

    def public(self, row):
        return {k: row[k] for k in ('id','user_id','title','kb','format','status','reviewer_id','review_note','created_at','reviewed_at')} | {'preview': json.loads(row['chunks']), 'source_notice': '上传参考资料；管理员审核不等于厂家认证或正式企业制度发布。'}

    def submit(self, user_id, title, kb, suffix, raw):
        chunks = extract_blocks(raw, suffix)
        key = uuid4().hex
        relative = f'data/knowledge/uploads/{key}.{suffix}'
        (self.root/relative).write_bytes(raw)
        with self.connect() as db:
            db.execute('INSERT INTO submissions VALUES (?,?,?,?,?,?,?,?,?,NULL,?,?,NULL)', (key,user_id,title,kb,suffix,relative,digest(raw),json.dumps(chunks,ensure_ascii=False),'pending','',datetime.now(UTC).isoformat()))
        return next(r for r in self.items(user_id) if r['id'] == key)

    def review(self, key, admin_id, approved, note, runtime):
        with REVIEW_LOCK:
            with self.connect() as db:
                row = db.execute('SELECT * FROM submissions WHERE id=?', (key,)).fetchone()
                if row is None: raise HTTPException(404, '上传资料不存在')
                row = dict(row)
                if row['status'] == 'indexed':
                    if not approved: raise HTTPException(409, '资料已入库，不能通过待审操作撤回')
                    return self.public(row)
                if row['status'] == 'rejected': raise HTTPException(409, '资料已驳回，请修改后重新上传')
                if digest((self.root/row['path']).read_bytes()) != row['sha']: raise HTTPException(409, '上传原件已变化，不能沿用审核')
                reviewed_at = datetime.now(UTC).isoformat()
                db.execute('UPDATE submissions SET status=?,reviewer_id=?,review_note=?,reviewed_at=? WHERE id=?', ('approved' if approved else 'rejected',admin_id,note,reviewed_at,key))
            if not approved:
                # Recover a publication interrupted between registry write and index completion.
                # Removing source eligibility immediately hides any interrupted generation too.
                registry_path = self.directory/'approved.json'
                if registry_path.exists():
                    registry = json.loads(registry_path.read_text(encoding='utf-8'))
                    if any(r['id'] == key for r in registry['documents']):
                        registry['documents'] = [r for r in registry['documents'] if r['id'] != key]
                        temporary = registry_path.with_suffix('.tmp')
                        temporary.write_text(json.dumps(registry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
                        temporary.replace(registry_path)
            if approved:
                registry_path = self.directory/'approved.json'
                previous = registry_path.read_bytes() if registry_path.exists() else None
                registry = json.loads(previous) if previous else {'documents': []}
                registry['documents'] = [r for r in registry['documents'] if r['id'] != key]+[{k: row[k] for k in ('id','title','kb','format','path','sha')} | {'reviewer_id': admin_id, 'reviewed_at': reviewed_at, 'chunks': json.loads(row['chunks'])}]
                temporary = registry_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(registry,ensure_ascii=False,indent=2)+'\n',encoding='utf-8'); temporary.replace(registry_path)
                try:
                    runtime.rebuild()
                except (KnowledgeError, OSError, ValueError, ChromaError):
                    if previous is None: registry_path.unlink(missing_ok=True)
                    else:
                        temporary.write_bytes(previous); temporary.replace(registry_path)
                    with self.connect() as db: db.execute("UPDATE submissions SET status='index_failed' WHERE id=?", (key,))
                    raise HTTPException(503, '资料已审核，但索引失败；未建立新资料检索能力，管理员可重试索引') from None
                with self.connect() as db: db.execute("UPDATE submissions SET status='indexed' WHERE id=?", (key,))
            return next(r for r in self.items(admin_id, True) if r['id'] == key)


class Review(StrictModel):
    approved: bool
    note: str = Field(default='', max_length=500)


@router.get('')
def listing(request: Request, user=Depends(current_user)):
    return {'items': SubmissionStore(request.app.state.knowledge.root).items(user.id, user.role == 'admin')}


@router.post('', status_code=201)
async def upload(request: Request, title: str = Query(min_length=1,max_length=100), knowledge_base: str = Query(), format: str = Query(), user=Depends(current_user)):
    if knowledge_base not in CORPORA or format not in {'txt','md','pdf','docx'}: raise HTTPException(422, '知识域或文件格式不支持')
    if not title.strip(): raise HTTPException(422, '请填写资料标题')
    content = bytearray()
    async for chunk in request.stream():
        content.extend(chunk)
        if len(content)>10*1024*1024: raise HTTPException(413,'知识文档最大10MB')
    return await asyncio.to_thread(SubmissionStore(request.app.state.knowledge.root).submit,user.id,title.strip(),knowledge_base,format,bytes(content))


@router.post('/{key}/review')
async def review(key: str, payload: Review, request: Request, user=Depends(administrator)):
    return await asyncio.to_thread(SubmissionStore(request.app.state.knowledge.root).review,key,user.id,payload.approved,payload.note,request.app.state.knowledge)
