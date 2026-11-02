
from enum import Enum
from pydantic import BaseModel, ConfigDict, Field


class CapabilityRisk(str, Enum):
    READ = "read"
    WRITE = "write"
    DESTRUCTIVE = "destructive"


class ConnectorCapability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    provider: str = Field(min_length=1)
    description: str = Field(min_length=1)

    risk: CapabilityRisk

    required_permissions: set[str] = Field(default_factory=set)

    requires_approval: bool = False
