"""Persist appointment requests before a date is agreed."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("booking_requests",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("reference", sa.String(12), nullable=False),
        sa.Column("patient_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("email", sa.String(255)),
        sa.Column("submission_key", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"), sa.UniqueConstraint("reference"), sa.UniqueConstraint("submission_key"),
        sa.ForeignKeyConstraint(["patient_id"], ["patients.id"], ondelete="RESTRICT"),
        sa.CheckConstraint("status IN ('pendente','em_contato','concluida','cancelada')", name="ck_booking_requests_status"))
    for column in ("patient_id", "status", "created_at"):
        op.create_index("ix_booking_requests_" + column, "booking_requests", [column])


def downgrade():
    op.drop_table("booking_requests")
