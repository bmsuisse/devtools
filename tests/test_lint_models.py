from pathlib import Path

from bmsdna.devtools.lint_models import check_models_file

_BIG_MODEL = """
from pydantic import BaseModel

class ContractsModel(BaseModel):
    id: int
    name: str
    status: str
    customer_id: str
    amount: float
    currency: str
    created_at: str
"""

_SMALL_MODEL = """
from pydantic import BaseModel

class CorrectRequest(BaseModel):
    text: str
"""


def _write(tmp_path: Path, rel: str, source: str) -> Path:
    path = tmp_path / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)
    return path


def test_big_model_directly_under_api_is_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "backend/api/contracts.py", _BIG_MODEL)
    findings = check_models_file(path, repo_root=tmp_path)
    assert len(findings) == 1
    assert findings[0].rule == "pydantic-model-misplaced"
    assert "ContractsModel" in findings[0].message


def test_big_model_under_api_models_is_not_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "backend/api/models/contracts.py", _BIG_MODEL)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_big_model_under_api_schemas_is_not_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "backend/api/schemas/contracts.py", _BIG_MODEL)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_big_model_under_unlisted_api_subdir_is_flagged(tmp_path: Path) -> None:
    # Real CCMT2 layout: backend/api/tree/search.py -- "tree" isn't an allowed subdir.
    path = _write(tmp_path, "backend/api/tree/search.py", _BIG_MODEL)
    assert len(check_models_file(path, repo_root=tmp_path)) == 1


def test_big_model_outside_any_api_tree_is_not_flagged(tmp_path: Path) -> None:
    path = _write(tmp_path, "backend/db/models.py", _BIG_MODEL)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_small_model_under_api_is_not_flagged(tmp_path: Path) -> None:
    # Real OneSales pattern: a 1-field request DTO defined right next to its route.
    path = _write(tmp_path, "backend/api/voice.py", _SMALL_MODEL)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_field_count_boundary_is_exclusive(tmp_path: Path) -> None:
    exactly_five = """
from pydantic import BaseModel

class Exactly5(BaseModel):
    a: int
    b: int
    c: int
    d: int
    e: int
"""
    path = _write(tmp_path, "backend/api/thing.py", exactly_five)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_classvar_fields_are_not_counted(tmp_path: Path) -> None:
    source = """
from typing import ClassVar
from pydantic import BaseModel

class WithClassVars(BaseModel):
    a: int
    b: int
    c: int
    d: int
    e: int
    TABLE_NAME: ClassVar[str] = "things"
    SCHEMA: ClassVar[str] = "public"
"""
    path = _write(tmp_path, "backend/api/thing.py", source)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_non_pydantic_class_is_ignored(tmp_path: Path) -> None:
    source = """
class PlainClass:
    a: int
    b: int
    c: int
    d: int
    e: int
    f: int
"""
    path = _write(tmp_path, "backend/api/thing.py", source)
    assert check_models_file(path, repo_root=tmp_path) == []


def test_syntax_error_file_is_skipped(tmp_path: Path) -> None:
    path = tmp_path / "backend" / "api" / "bad.py"
    path.parent.mkdir(parents=True)
    assert check_models_file(path, repo_root=tmp_path, source="def f(:\n") == []


def test_non_utf8_file_is_skipped_not_crashed(tmp_path: Path) -> None:
    path = tmp_path / "backend" / "api" / "bad_encoding.py"
    path.parent.mkdir(parents=True)
    path.write_bytes(b"\xff\xfe# not valid utf-8\n")
    assert check_models_file(path, repo_root=tmp_path) == []
