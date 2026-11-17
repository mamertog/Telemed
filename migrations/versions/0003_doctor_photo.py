"""Install the doctor portrait supplied by the site owner."""
import hashlib
from pathlib import Path
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    data = (Path(__file__).resolve().parents[2] / "app" / "assets" / "juliana.jpeg").read_bytes()
    assets = sa.table("media_assets", sa.column("key", sa.String), sa.column("content_type", sa.String), sa.column("data", sa.LargeBinary), sa.column("sha256", sa.String), sa.column("updated_at", sa.DateTime(timezone=True)))
    statement = insert(assets).values(key="doctor_photo", content_type="image/jpeg", data=data, sha256=hashlib.sha256(data).hexdigest(), updated_at=sa.func.now())
    op.get_bind().execute(statement.on_conflict_do_update(index_elements=[assets.c.key], set_={"content_type": statement.excluded.content_type, "data": statement.excluded.data, "sha256": statement.excluded.sha256, "updated_at": sa.func.now()}))


def downgrade():
    # Preserve the uploaded portrait when rolling back application code.
    pass
