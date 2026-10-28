import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi.testclient import TestClient
from pydantic import SecretStr
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, SQLAlchemyError

from backend.app.config import ROOT, Settings
from backend.app.db import make_engine
from backend.app.main import create_app


def test_liveness_without_database_or_ai_credentials():
    settings = Settings(_env_file=None, mysql_user="", mysql_password="")
    with TestClient(create_app(settings)) as client:
        assert client.get("/api/health").json()["status"] == "ok"
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert response.json()["database"] == "unavailable"


def test_database_url_preserves_special_characters_without_exposing_password():
    password = "example@:/password"
    settings = Settings(
        _env_file=None, mysql_user="probe", mysql_password=SecretStr(password)
    )
    assert settings.database_url().password == password
    assert password not in str(settings.database_url())
    assert password not in repr(settings)


def test_readiness_does_not_expose_database_errors(monkeypatch):
    settings = Settings(_env_file=None, mysql_user="probe", mysql_password="example")
    application = create_app(settings)
    with TestClient(application) as client:
        def fail():
            raise SQLAlchemyError("sensitive-database-error")

        monkeypatch.setattr(application.state.engine, "connect", fail)
        response = client.get("/api/ready")
        assert response.status_code == 503
        assert "sensitive-database-error" not in response.text
        assert client.get("/api/health").status_code == 200


@pytest.mark.mysql
def test_mysql_test_database_and_readiness_match_migration_head():
    settings = Settings()
    if not settings.test_mysql_user or not settings.test_mysql_password.get_secret_value():
        pytest.skip("Project test database credentials are not configured")
    assert settings.test_mysql_database != settings.mysql_database
    assert settings.test_mysql_user != settings.mysql_user
    engine = make_engine(settings, test=True)
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT DATABASE()")).scalar_one() == settings.test_mysql_database
            expected = ScriptDirectory.from_config(Config(str(ROOT / "alembic.ini"))).get_current_head()
            assert MigrationContext.configure(connection).get_current_revision() == expected
    finally:
        engine.dispose()
    test_settings = settings.model_copy(update={
        "mysql_user": settings.test_mysql_user,
        "mysql_password": settings.test_mysql_password,
        "mysql_database": settings.test_mysql_database,
    })
    with TestClient(create_app(test_settings)) as client:
        assert client.get("/api/ready").json() == {"status": "ok", "database": "connected"}


@pytest.mark.mysql
def test_mysql_development_and_test_accounts_are_isolated():
    settings = Settings()
    if not settings.test_mysql_user or not settings.test_mysql_password.get_secret_value():
        pytest.skip("Project test database credentials are not configured")
    assert (settings.mysql_database, settings.test_mysql_database) == ("fours_agent_dev", "fours_agent_test")
    for use_test, other in ((False, "fours_agent_test"), (True, "fours_agent_dev")):
        engine = make_engine(settings, test=use_test)
        try:
            with engine.connect() as connection:
                with pytest.raises(DBAPIError) as error:
                    connection.exec_driver_sql(f"USE `{other}`")
                assert error.value.orig.args[0] == 1044
        finally:
            engine.dispose()
