from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=ROOT / ".env", extra="ignore")
    data_dir: Path = ROOT / "data"
    max_upload_mb: int = 50
    max_uncompressed_mb: int = 200
    max_zip_entries: int = 4096
    calculation_timeout: int = 120
    job_timeout: int = 300
    print_timeout: int = 60
    mathcad_progid: str = "MathcadPrime.Application"
    mathcad_install_dir: Path = Path("C:/Program Files/PTC/Mathcad Prime 12.0.1.0")
    print_config: Path = ROOT / "config" / "print-uia.json"
    result_strategy: str = "api_recalculate"
    integration_verified: bool = False
    matrix_xml_order: str | None = None

    def directory(self) -> Path:
        path = (self.data_dir if self.data_dir.is_absolute() else ROOT / self.data_dir).resolve()
        path.mkdir(parents=True, exist_ok=True)
        return path


@lru_cache
def settings() -> Settings:
    return Settings()
