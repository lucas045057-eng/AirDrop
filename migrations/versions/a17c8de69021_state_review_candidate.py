"""persist encrypted native state candidates for manual review"""
from alembic import op
import sqlalchemy as sa

revision = 'a17c8de69021'
down_revision = 'f2a5c81d9043'
branch_labels = None
depends_on = None


def upgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE executions ADD COLUMN state_review_artifact_id VARCHAR(32) REFERENCES artifacts(id)')
    else:
        op.add_column('executions', sa.Column('state_review_artifact_id', sa.String(32), nullable=True))
        op.create_foreign_key('fk_executions_state_review_artifact_id', 'executions', 'artifacts',
                              ['state_review_artifact_id'], ['id'])


def downgrade():
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE executions DROP COLUMN state_review_artifact_id')
    else:
        op.drop_constraint('fk_executions_state_review_artifact_id', 'executions', type_='foreignkey')
        op.drop_column('executions', 'state_review_artifact_id')
