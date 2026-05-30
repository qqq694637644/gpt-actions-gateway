from __future__ import annotations

from pydantic import Field

from app.models.common import GatewayBaseModel


class TreeEntry(GatewayBaseModel):
    path: str
    type: str = Field(description="file, dir, symlink, or submodule")
    size: int | None = None
    sha: str | None = None


class ListTreeResponse(GatewayBaseModel):
    ref: str
    path: str | None = None
    entries: list[TreeEntry]
    truncated: bool = False
    max_results: int


class ReadFileRequest(GatewayBaseModel):
    ref: str = Field(default="main", description="Branch name or exact commit SHA to read from.")
    path: str

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [{"ref": "main", "path": "src/app.py"}],
        }
    }


class FileContentResponse(GatewayBaseModel):
    path: str
    ref: str
    sha: str
    size: int
    content: str
    truncated: bool = False
    binary: bool = False
    encoding: str = "utf-8"
    suggestion: str | None = None


class ReadFileRangeRequest(GatewayBaseModel):
    ref: str = "main"
    path: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [{"ref": "main", "path": "src/app.py", "start_line": 1, "end_line": 120}],
        }
    }


class FileRangeResponse(GatewayBaseModel):
    path: str
    ref: str
    start_line: int
    end_line: int
    total_lines: int
    content: str
    truncated: bool = False


class ReadFilesRequest(GatewayBaseModel):
    ref: str = "main"
    paths: list[str] = Field(min_length=1, max_length=20)

    model_config = GatewayBaseModel.model_config | {
        "json_schema_extra": {
            "examples": [{"ref": "main", "paths": ["pyproject.toml", "app/main.py"]}],
        }
    }


class ReadFilesResponse(GatewayBaseModel):
    ref: str
    files: list[FileContentResponse]
    total_bytes: int
    truncated: bool = False
