"""Keep formal execution idempotency after history deletion."""
from alembic import op
import sqlalchemy as sa

revision = '71a960e315dc'
down_revision = '3c6a61ccad54'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('execution_receipts',
        sa.Column('task_id', sa.String(32), sa.ForeignKey('tasks.id'), primary_key=True),
        sa.Column('period', sa.String(200), primary_key=True),
        sa.Column('execution_id', sa.String(32), sa.ForeignKey('executions.id', ondelete='SET NULL'), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False))
    op.execute(sa.text('INSERT INTO execution_receipts (task_id, period, execution_id, created_at) '
                       'SELECT task_id, period, id, created_at FROM executions WHERE is_test = false'))


def downgrade():
    op.drop_table('execution_receipts')
