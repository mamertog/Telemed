"""Initialize the agreed schedule only on a fresh database."""
from datetime import time
import sqlalchemy as sa
from app import create_app
from app.extensions import db
from app.models import AvailabilityRule, Service

app = create_app()
with app.app_context():
    if not db.session.execute(sa.select(AvailabilityRule.id).limit(1)).first():
        for weekday in range(6):
            db.session.add(AvailabilityRule(weekday=weekday, start_time=time(13), end_time=time(20), active=True))
        service = db.session.execute(sa.select(Service).order_by(Service.position, Service.id)).scalars().first()
        if service:
            service.duration_minutes = 30
            service.active = True
        db.session.commit()
        print("Initial schedule configured: Monday-Saturday, 13:00-20:00, 30 minutes.")
    else:
        print("Existing schedule preserved.")
