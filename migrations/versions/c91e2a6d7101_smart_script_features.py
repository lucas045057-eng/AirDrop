"""Add analysis jobs, encrypted transformation drafts and nullable feature task linkage."""
from alembic import op
import sqlalchemy as sa

revision = 'c91e2a6d7101'
down_revision = '71a960e315dc'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('script_analysis_jobs',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('source_version_id', sa.String(32), sa.ForeignKey('script_versions.id'), nullable=False),
        sa.Column('source_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('request_key', sa.String(100), nullable=False),
        sa.Column('operation', sa.String(20), nullable=False), sa.Column('status', sa.String(40), nullable=False),
        sa.Column('config', sa.JSON(), nullable=False), sa.Column('result', sa.JSON(), nullable=False),
        sa.Column('preview_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id')),
        sa.Column('preview_sha256', sa.String(64)), sa.Column('reviewed_preview_sha256', sa.String(64)),
        sa.Column('ai_sent', sa.Boolean(), nullable=False), sa.Column('cancel_requested', sa.Boolean(), nullable=False),
        sa.Column('lease_token', sa.String(32)), sa.Column('lease_until', sa.Float()),
        sa.Column('attempts', sa.Integer(), nullable=False), sa.Column('error', sa.Text(), nullable=False),
        sa.Column('created_by', sa.String(32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False), sa.Column('started_at', sa.Float()), sa.Column('finished_at', sa.Float()),
        sa.UniqueConstraint('source_version_id', 'request_key'))
    for column in ['source_version_id', 'status', 'lease_until']:
        op.create_index('ix_script_analysis_jobs_' + column, 'script_analysis_jobs', [column])
    op.create_table('script_transformation_drafts',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('job_id', sa.String(32), sa.ForeignKey('script_analysis_jobs.id'), nullable=False, unique=True),
        sa.Column('source_version_id', sa.String(32), sa.ForeignKey('script_versions.id'), nullable=False),
        sa.Column('source_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('source_sha256', sa.String(64), nullable=False),
        sa.Column('output_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('diff_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('output_sha256', sa.String(64), nullable=False), sa.Column('review_sha256', sa.String(64), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False), sa.Column('status', sa.String(30), nullable=False),
        sa.Column('result_version_id', sa.String(32), sa.ForeignKey('script_versions.id')), sa.Column('created_at', sa.Float(), nullable=False))
    op.create_index('ix_script_transformation_drafts_source_version_id', 'script_transformation_drafts', ['source_version_id'])
    op.create_table('script_features',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('version_id', sa.String(32), sa.ForeignKey('script_versions.id')),
        sa.Column('draft_id', sa.String(32), sa.ForeignKey('script_transformation_drafts.id')),
        sa.Column('feature_key', sa.String(64), nullable=False), sa.Column('definition', sa.JSON(), nullable=False),
        sa.Column('code_sha256', sa.String(64), nullable=False), sa.Column('approval_sha256', sa.String(64)),
        sa.Column('version_manifest_sha256', sa.String(64)), sa.Column('cipher_sha256', sa.String(64)),
        sa.Column('status', sa.String(30), nullable=False), sa.Column('approved_by', sa.String(32), sa.ForeignKey('users.id')),
        sa.Column('approved_at', sa.Float()), sa.UniqueConstraint('version_id', 'feature_key'), sa.UniqueConstraint('draft_id', 'feature_key'),
        sa.CheckConstraint('(version_id IS NULL) <> (draft_id IS NULL)', name='ck_feature_one_owner'))
    for column in ['version_id', 'draft_id']:
        op.create_index('ix_script_features_' + column, 'script_features', [column])
    # SQLite can add a nullable inline reference without rebuilding the referenced
    # tasks table. Batch-copy would fail when existing executions reference tasks.
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE tasks ADD COLUMN script_feature_id VARCHAR(32) CONSTRAINT fk_tasks_script_feature_id REFERENCES script_features(id)')
        op.add_column('tasks', sa.Column('feature_config_snapshot', sa.JSON(), nullable=True))
    else:
        op.add_column('tasks', sa.Column('script_feature_id', sa.String(32), nullable=True))
        op.add_column('tasks', sa.Column('feature_config_snapshot', sa.JSON(), nullable=True))
        op.create_foreign_key('fk_tasks_script_feature_id', 'tasks', 'script_features', ['script_feature_id'], ['id'])


def downgrade():
    # The operator must back up and remove V2.1 feature tasks before downgrade.
    if op.get_bind().execute(sa.text('SELECT COUNT(*) FROM tasks WHERE script_feature_id IS NOT NULL')).scalar():
        raise RuntimeError('仍有功能级任务，禁止丢失配置；请使用升级前备份恢复或先清理这些任务')
    if op.get_bind().dialect.name == 'sqlite':
        op.execute('ALTER TABLE tasks DROP COLUMN feature_config_snapshot')
        op.execute('ALTER TABLE tasks DROP COLUMN script_feature_id')
    else:
        op.drop_constraint('fk_tasks_script_feature_id', 'tasks', type_='foreignkey')
        op.drop_column('tasks', 'feature_config_snapshot')
        op.drop_column('tasks', 'script_feature_id')
    op.drop_table('script_features')
    op.drop_table('script_transformation_drafts')
    op.drop_table('script_analysis_jobs')
