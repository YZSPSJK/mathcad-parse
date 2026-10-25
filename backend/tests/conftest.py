import os
from pathlib import Path
from uuid import uuid4

os.environ["DATA_DIR"] = str(Path(__file__).resolve().parents[2] / ".cache" / f"tests-{uuid4()}")
os.environ["MATRIX_XML_ORDER"] = "column-major"

import pytest  # noqa: E402

from backend.db import Base, engine, Session  # noqa: E402


@pytest.fixture(autouse=True)
def database():
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)
    yield


@pytest.fixture
def session():
    with Session() as handle:
        yield handle
