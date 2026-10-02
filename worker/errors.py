"""워커 오류 코드 (03-PIPELINE-SPEC 이벤트 오류 코드)."""
from __future__ import annotations


class NBError(Exception):
    code = "E_INTERNAL"

    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        if code:
            self.code = code
        self.message = message


class CodecError(NBError):
    code = "E_CODEC"


class EncoderError(NBError):
    code = "E_ENCODER"


class ModelHashError(NBError):
    code = "E_MODEL_HASH"


class ModelLicenseError(NBError):
    code = "E_MODEL_LICENSE"


class DiskError(NBError):
    code = "E_DISK"


class Cancelled(NBError):
    code = "E_CANCELLED"


class AuditExposure(NBError):
    code = "E_AUDIT_EXPOSURE"


class PathNotAllowed(NBError):
    code = "E_PATH"
