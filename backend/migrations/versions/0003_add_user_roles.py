"""add user roles and permissions

Revision ID: 0003_add_user_roles
Revises: 0002_plan_core
Create Date: 2026-10-08 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa


revision = '0003_add_user_roles'
down_revision = '0002_plan_core'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # สร้างตาราง roles สำหรับกำหนดบทบาทสิทธิ์
    op.create_table(
        'roles',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('name', sa.String(length=50), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('name')
    )

    # เพิ่มคอลัมน์ role_id ให้กับตาราง users เพื่อผูกสิทธิ์
    op.add_column('users', sa.Column('role_id', sa.Integer(), nullable=True))
    op.create_foreign_key('fk_users_roles', 'users', 'roles', ['role_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_users_roles', 'users', type_='foreignkey')
    op.drop_column('users', 'role_id')
    op.drop_table('roles')