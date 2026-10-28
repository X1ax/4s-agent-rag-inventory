from functools import lru_cache
from pathlib import Path

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy import URL

ROOT = Path(__file__).resolve().parents[2]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")

    mysql_host: str = "127.0.0.1"
    mysql_port: int = Field(default=3306, ge=1, le=65535)
    mysql_user: str = ""
    mysql_password: SecretStr = SecretStr("")
    mysql_database: str = "fours_agent_dev"
    test_mysql_user: str = ""
    test_mysql_password: SecretStr = SecretStr("")
    test_mysql_database: str = "fours_agent_test"
    jwt_secret: SecretStr = SecretStr("")
    api_key: SecretStr = SecretStr("")
    tavily_api_key: SecretStr = SecretStr("")
    base_url: str = ""
    model_id: str = ""
    embedding_base_url: str = "https://api.siliconflow.cn/v1"
    embedding_model_id: str = "BAAI/bge-m3"
    embedding_api_key: SecretStr = SecretStr("")

    def database_url(self, *, test: bool = False) -> URL:
        user = self.test_mysql_user if test else self.mysql_user
        password = self.test_mysql_password if test else self.mysql_password
        database = self.test_mysql_database if test else self.mysql_database
        if not user or not password.get_secret_value() or not database:
            raise ValueError("MySQL configuration is incomplete")
        return URL.create(
            "mysql+pymysql",
            username=user,
            password=password.get_secret_value(),
            host=self.mysql_host,
            port=self.mysql_port,
            database=database,
            query={"charset": "utf8mb4"},
        )


@lru_cache
def get_settings() -> Settings:
    return Settings()
