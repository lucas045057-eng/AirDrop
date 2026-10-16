"""keep raw process output size separate from stored redacted log size"""
from alembic import op
import sqlalchemy as sa

revision = 'b9650e73a1c4'
down_revision = 'a17c8de69021'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('execution_attempts', sa.Column('raw_output_bytes', sa.Integer(), nullable=False,
                                                  server_default=sa.text('0')))


def downgrade():
    op.drop_column('execution_attempts', 'raw_output_bytes')
