"""native runtime definitions, project accounts, states, builds and log segments"""
from alembic import op
import sqlalchemy as sa

revision = 'f2a5c81d9043'
down_revision = 'c91e2a6d7101'
branch_labels = None
depends_on = None

# 旧记录必须保持可读：新增列全部带默认值或以空值兼容，原分析审核不自动等于运行审核。
ADDED_COLUMNS = {
    'script_versions': [('runtime_hints', sa.JSON(), None)],
    'tasks': [('execution_backend', sa.String(20), "'legacy_mock'"), ('wallet_selection', sa.JSON(), None)],
    'script_features': [('runtime_spec', sa.JSON(), None), ('runtime_status', sa.String(30), "'DRAFT'"),
                        ('runtime_review_sha256', sa.String(64), None), ('runtime_approved_at', sa.Float(), None)],
    'executions': [('execution_backend', sa.String(20), "'legacy_mock'"), ('request_key', sa.String(100), None),
                   ('runtime_snapshot', sa.JSON(), None), ('state_generation', sa.Integer(), None),
                   ('state_status', sa.String(30), "''"), ('log_status', sa.String(30), "''")],
    'execution_attempts': [('runner_handle', sa.String(200), None), ('lease_generation', sa.Integer(), '0'),
                           ('log_bytes', sa.Integer(), '0')],
}
REFERENCE_COLUMNS = [
    ('script_features', 'runtime_approved_by', 'users'),
    ('script_features', 'runtime_build_id', 'runtime_builds'),
    ('executions', 'input_snapshot_artifact_id', 'artifacts'),
]


def _add_column(table, name, type_, default):
    column = sa.Column(name, type_, nullable=default is None,
                       server_default=sa.text(default) if default is not None else None)
    op.add_column(table, column)


def upgrade():
    # 先建被引用表，再补脚本功能上的外键列。
    op.create_table('runtime_builds',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('package_id', sa.String(32), sa.ForeignKey('script_packages.id'), nullable=True),
        sa.Column('environment_key', sa.String(200), nullable=False),
        sa.Column('python_version', sa.String(20), nullable=False),
        sa.Column('base_image', sa.String(200), nullable=False),
        sa.Column('dependency_files', sa.JSON(), nullable=False),
        sa.Column('lock_artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=True),
        sa.Column('lock_sha256', sa.String(64), nullable=True),
        sa.Column('image_id', sa.String(200), nullable=True),
        sa.Column('status', sa.String(30), nullable=False),
        sa.Column('log', sa.Text(), nullable=False),
        sa.Column('error', sa.Text(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('started_at', sa.Float(), nullable=True),
        sa.Column('finished_at', sa.Float(), nullable=True),
        sa.UniqueConstraint('environment_key', name='uq_runtime_builds_environment_key'))
    for column in ['package_id', 'status']:
        op.create_index('ix_runtime_builds_' + column, 'runtime_builds', [column])
    op.create_table('project_accounts',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('project_id', sa.String(32), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('package_id', sa.String(32), sa.ForeignKey('script_packages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('wallet_id', sa.String(32), sa.ForeignKey('wallets.id'), nullable=False),
        sa.Column('public_config', sa.JSON(), nullable=False),
        sa.Column('encrypted_config', sa.Text(), nullable=False),
        sa.Column('revision', sa.Integer(), nullable=False),
        sa.Column('source', sa.String(200), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('project_id', 'package_id', 'wallet_id', name='uq_project_accounts_scope'))
    for column in ['project_id', 'package_id', 'wallet_id']:
        op.create_index('ix_project_accounts_' + column, 'project_accounts', [column])
    op.create_table('project_script_states',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('project_id', sa.String(32), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('package_id', sa.String(32), sa.ForeignKey('script_packages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('state_schema_version', sa.Integer(), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=True),
        sa.Column('artifact_sha256', sa.String(64), nullable=True),
        sa.Column('locked_execution_id', sa.String(32), sa.ForeignKey('executions.id'), nullable=True),
        sa.Column('updated_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('project_id', 'package_id', 'state_schema_version', name='uq_project_script_states_scope'))
    for column in ['project_id', 'package_id']:
        op.create_index('ix_project_script_states_' + column, 'project_script_states', [column])
    op.create_table('account_import_drafts',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('project_id', sa.String(32), sa.ForeignKey('projects.id', ondelete='CASCADE'), nullable=False),
        sa.Column('package_id', sa.String(32), sa.ForeignKey('script_packages.id', ondelete='CASCADE'), nullable=False),
        sa.Column('artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('file_sha256', sa.String(64), nullable=False),
        sa.Column('filename', sa.String(200), nullable=False),
        sa.Column('field_mapping', sa.JSON(), nullable=False),
        sa.Column('preview', sa.JSON(), nullable=False),
        sa.Column('preview_sha256', sa.String(64), nullable=False),
        sa.Column('created_by', sa.String(32), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('expires_at', sa.Float(), nullable=False),
        sa.Column('consumed_at', sa.Float(), nullable=True),
        sa.Column('result', sa.JSON(), nullable=True),
        sa.Column('created_at', sa.Float(), nullable=False))
    for column in ['project_id', 'package_id']:
        op.create_index('ix_account_import_drafts_' + column, 'account_import_drafts', [column])
    op.create_table('execution_log_segments',
        sa.Column('id', sa.String(32), primary_key=True),
        sa.Column('attempt_id', sa.String(32), sa.ForeignKey('execution_attempts.id', ondelete='CASCADE'), nullable=False),
        sa.Column('seq', sa.Integer(), nullable=False),
        sa.Column('artifact_id', sa.String(32), sa.ForeignKey('artifacts.id'), nullable=False),
        sa.Column('artifact_sha256', sa.String(64), nullable=False),
        sa.Column('offset_bytes', sa.Integer(), nullable=False),
        sa.Column('size_bytes', sa.Integer(), nullable=False),
        sa.Column('created_at', sa.Float(), nullable=False),
        sa.UniqueConstraint('attempt_id', 'seq', name='uq_execution_log_segments_attempt_seq'))
    op.create_index('ix_execution_log_segments_attempt_id', 'execution_log_segments', ['attempt_id'])
    for table, columns in ADDED_COLUMNS.items():
        for name, type_, default in columns:
            _add_column(table, name, type_, default)
    for table, name, target in REFERENCE_COLUMNS:
        if op.get_bind().dialect.name == 'sqlite':
            # SQLite 允许以内联引用追加可空列；重建表会破坏已有执行记录的外键。
            op.execute(f'ALTER TABLE {table} ADD COLUMN {name} VARCHAR(32) REFERENCES {target}(id)')
        else:
            _add_column(table, name, sa.String(32), None)
            op.create_foreign_key(f'fk_{table}_{name}', table, target, [name], ['id'])


def downgrade():
    bind = op.get_bind()
    sqlite = bind.dialect.name == 'sqlite'
    if sqlite:
        for table, name, _ in REFERENCE_COLUMNS:
            op.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
    else:
        for table, name, _ in REFERENCE_COLUMNS:
            op.drop_constraint(f'fk_{table}_{name}', table, type_='foreignkey')
            op.drop_column(table, name)
    for table, columns in ADDED_COLUMNS.items():
        for name, _, _ in columns:
            if sqlite:
                op.execute(f'ALTER TABLE {table} DROP COLUMN {name}')
            else:
                op.drop_column(table, name)
    op.drop_table('execution_log_segments')
    op.drop_table('account_import_drafts')
    op.drop_table('project_script_states')
    op.drop_table('project_accounts')
    op.drop_table('runtime_builds')
