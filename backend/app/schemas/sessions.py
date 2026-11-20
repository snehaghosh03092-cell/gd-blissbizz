from pydantic import BaseModel


class CsrfResponse(BaseModel):
    csrf_token: str
