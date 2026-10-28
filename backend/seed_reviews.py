"""Synthetic history only: record role-separated simulated approvals explicitly."""
from sqlalchemy import select

from backend.app.models import Document, User
from backend.app.reviews import decide_review, review_view, submit_review


def review_simulated_document(session, document_id, creator_id):
    document = session.get(Document, document_id)
    if document.status != 'draft' or review_view(session, document)['status'] == 'approved':
        return  # Do not backfill approvals on already-effective historical records.
    manager = session.scalar(select(User).where(User.role == 'manager', User.active.is_(True), User.id != creator_id).order_by(User.id))
    if manager is None:
        raise RuntimeError('生成模拟业务前需有独立的启用经理账号，不能绕过审核')
    submit_review(session, document_id, creator_id)
    decide_review(session, document_id, manager.id, True, '模拟历史生成的角色分离审核记录；不代表真实企业批准')
