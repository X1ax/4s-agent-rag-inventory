"""Server-owned proposals, atomically committed with business results."""
from alembic import op
import sqlalchemy as sa

revision = "0006_agent"
down_revision = "0005_suppliers"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('agent_proposals',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('thread_id', sa.String(32), nullable=False),
        sa.Column('turn_id', sa.String(32), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('action', sa.String(40), nullable=False),
        sa.Column('payload', sa.JSON(), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.UniqueConstraint('thread_id', 'turn_id', name='uq_agent_proposal_turn'))
    op.create_index('ix_agent_proposals_user_id', 'agent_proposals', ['user_id'])
    op.create_index('ix_agent_proposals_thread_id', 'agent_proposals', ['thread_id'])


def downgrade():
    if op.get_bind().scalar(sa.text('SELECT COUNT(*) FROM agent_proposals')):
        raise RuntimeError('已有提案与执行记录，禁止回退丢失审核来源')
    op.drop_table('agent_proposals')
