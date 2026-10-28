from alembic import context

from backend.app.config import get_settings
from backend.app.db import Base, make_engine
from backend.app import models  # Register actual tables for migration comparison.

settings = get_settings()
database = context.get_x_argument(as_dictionary=True).get("database", "dev")
if database not in {"dev", "test"}:
    raise ValueError("database must be dev or test")
use_test = database == "test"

if context.is_offline_mode():
    context.configure(
        url=settings.database_url(test=use_test),
        target_metadata=Base.metadata,
        literal_binds=True,
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(settings, test=use_test)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection, target_metadata=Base.metadata)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
