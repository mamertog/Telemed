"""Set the clinic WhatsApp contact requested by the owner."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, insert

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    settings = sa.table("site_settings", sa.column("key", sa.String), sa.column("value", JSONB), sa.column("updated_at", sa.DateTime(timezone=True)))
    statement = insert(settings).values(key="whatsapp", value="19989811414", updated_at=sa.func.now())
    op.get_bind().execute(statement.on_conflict_do_update(index_elements=[settings.c.key], set_={"value": statement.excluded.value, "updated_at": sa.func.now()}))


def downgrade():
    # Retain the contact setting: rollback must not erase administrator data.
    pass
