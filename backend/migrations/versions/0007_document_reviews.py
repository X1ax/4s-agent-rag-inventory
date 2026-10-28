"""Separate business review from document execution and Agent confirmation."""
from alembic import op
import sqlalchemy as sa

revision = '0007_reviews'
down_revision = '0006_agent'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('document_reviews',
        sa.Column('document_id', sa.Integer(), sa.ForeignKey('documents.id'), primary_key=True),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('submitted_by', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('submitted_at', sa.DateTime(), nullable=False),
        sa.Column('reviewer_id', sa.Integer(), sa.ForeignKey('users.id')),
        sa.Column('reviewed_at', sa.DateTime()),
        sa.Column('note', sa.String(500), nullable=False),
        sa.CheckConstraint("status IN ('pending','approved','rejected')", name='ck_document_review_status'))
    op.create_index('ix_document_reviews_status', 'document_reviews', ['status'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM document_reviews')):
        raise RuntimeError('存在业务审核记录，禁止回退丢失审核依据')
    op.drop_table('document_reviews')
