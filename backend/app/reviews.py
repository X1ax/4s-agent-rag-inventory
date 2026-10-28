"""Purchase/transfer review; approval itself never changes business stock/state."""
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select
from sqlalchemy.orm import Session, aliased

from backend.app.api import page_result
from backend.app.inventory import audit
from backend.app.models import Document, DocumentReview, User, Warehouse, utcnow
from backend.app.security import current_user, session_dependency, stock_writer

router = APIRouter(prefix='/api/reviews', tags=['审核中心'])


def review_view(session, document):
    row = session.get(DocumentReview, document.id)
    if row is None:
        return {'status': 'not_submitted' if document.status == 'draft' and document.kind in {'purchase','transfer'} else 'legacy',
            'submitted_by': None, 'reviewer_id': None, 'note': ''}
    return {key: getattr(row, key) for key in ('status','submitted_by','submitted_at','reviewer_id','reviewed_at','note')}


def require_review(session, document):
    if document.kind in {'purchase','transfer'} and review_view(session, document)['status'] != 'approved':
        raise HTTPException(409, '采购或调拨单须先提交审核，并由其他管理员或经理审核通过；请前往审核中心')


def locked_document(session, document_id):
    document = session.scalar(select(Document).where(Document.id == document_id).with_for_update())
    if document is None:
        raise HTTPException(404, '单据不存在')
    if document.kind not in {'purchase','transfer'}:
        raise HTTPException(409, '仅采购单或调拨单可以提交和处理审核')
    return document


def submit_review(session, document_id, user_id):
    user = session.get(User, user_id)
    if user is None or not user.active or user.role not in {'admin','storekeeper'}:
        raise HTTPException(403, '仅管理员或库管可提交本人创建的业务单据')
    document = locked_document(session, document_id)
    if document.status != 'draft':
        raise HTTPException(409, '仅草稿单据可以提交审核')
    if document.operator_id != user.id:
        raise HTTPException(403, '请由单据创建人核对后提交审核')
    row = session.get(DocumentReview, document_id)
    if row:
        if row.status == 'rejected':
            raise HTTPException(409, '单据已驳回，请取消草稿并按意见重新制单')
        return {'id': document_id, 'review_status': row.status, 'replayed': True}
    session.add(DocumentReview(document_id=document_id, status='pending', submitted_by=user.id, note=''))
    audit(session, user.id, 'review.submit', 'documents', document_id)
    session.flush()
    return {'id': document_id, 'review_status': 'pending', 'replayed': False}


def decide_review(session, document_id, user_id, approved, note=''):
    user = session.get(User, user_id)
    if user is None or not user.active or user.role not in {'admin','manager'}:
        raise HTTPException(403, '仅管理员或经理可审核采购单和调拨单')
    document = locked_document(session, document_id)
    row = session.get(DocumentReview, document_id)
    if row is None:
        raise HTTPException(409, '单据尚未提交审核')
    if user.id in {document.operator_id, row.submitted_by}:
        raise HTTPException(403, '不能审核本人创建或提交的业务单据，请交其他管理员或经理审核')
    target = 'approved' if approved else 'rejected'
    note = note.strip()
    if not approved and not note:
        raise HTTPException(422, '驳回时请填写审核意见')
    if row.status != 'pending':
        if row.status == target and row.reviewer_id == user.id and row.note == note:
            return {'id': document_id, 'review_status': row.status, 'replayed': True}
        raise HTTPException(409, '单据已完成审核，请刷新核对，不能覆盖原审核结论')
    if document.status != 'draft':
        raise HTTPException(409, '单据已取消或执行，不能再作出新的审核结论')
    row.status, row.reviewer_id, row.reviewed_at, row.note = target, user.id, utcnow(), note
    audit(session, user.id, 'review.approve' if approved else 'review.reject', 'documents', document_id)
    session.flush()
    return {'id': document_id, 'review_status': row.status, 'replayed': False}


class ReviewDecision(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    approved: bool
    note: str = Field(default='', max_length=500)


@router.get('/documents')
def listing(kind: Literal['purchase','transfer'], status: Literal['','not_submitted','pending','approved','rejected'] = '',
        page: int = Query(default=1, ge=1), page_size: int = Query(default=10, ge=1, le=100),
        user=Depends(current_user), session: Session = Depends(session_dependency)):
    reviewer, target = aliased(User), aliased(Warehouse)
    state = func.coalesce(DocumentReview.status, 'not_submitted')
    statement = select(Document.id, Document.kind, Document.status, Document.business_date, Document.operator_id,
        User.display_name.label('operator'), Warehouse.name.label('warehouse'), target.name.label('target_warehouse'),
        state.label('review_status'), DocumentReview.submitted_at, DocumentReview.reviewed_at,
        reviewer.display_name.label('reviewer'), DocumentReview.note.label('review_note')).join(User, User.id == Document.operator_id).join(
        Warehouse, Warehouse.id == Document.warehouse_id).outerjoin(target, target.id == Document.target_warehouse_id).outerjoin(
        DocumentReview, DocumentReview.document_id == Document.id).outerjoin(reviewer, reviewer.id == DocumentReview.reviewer_id).where(
        Document.kind == kind, (Document.status == 'draft') | DocumentReview.document_id.is_not(None))
    if user.role == 'storekeeper':
        statement = statement.where(Document.operator_id == user.id)
    if status:
        statement = statement.where(state == status)
    if status == 'pending':
        statement = statement.where(Document.status == 'draft')
    return page_result(session, statement.order_by(Document.id.desc()), page, page_size)


@router.post('/documents/{document_id}/submit')
def submit(document_id: int, user=Depends(stock_writer), session: Session = Depends(session_dependency)):
    result = submit_review(session, document_id, user.id)
    session.commit()
    return result


@router.post('/documents/{document_id}/decision')
def decision(document_id: int, payload: ReviewDecision, user=Depends(current_user), session: Session = Depends(session_dependency)):
    result = decide_review(session, document_id, user.id, payload.approved, payload.note)
    session.commit()
    return result
