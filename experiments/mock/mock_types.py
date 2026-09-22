from typing import Any, Optional
from pydantic import BaseModel


class Message(BaseModel):
    role: str
    content: Any = None

    class Config:
        extra = "allow"


class CreateMessageRequest(BaseModel):
    model: str
    messages: list[Message]
    max_tokens: int = 4096
    stream: bool = False
    system: Any = None
    tools: Any = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stop_sequences: Optional[list[str]] = None
    metadata: Any = None

    class Config:
        extra = "allow"


class ChainEvents(BaseModel):
    """Ordered list of assistant turn events parsed from a trace file."""

    events: list[dict] = []

    class Config:
        extra = "allow"


class ReplaySession(BaseModel):
    """A loaded trace, keyed by its identifier (leafUuid or sessionId)."""

    identifier: str
    main_chain: ChainEvents = ChainEvents()
    side_chains: list[ChainEvents] = []  # One per subagent invocation, in trace order
    
    # Maps a tool_use_id to (chain_label, next_turn_index)
    tool_to_next_event: dict[str, tuple[str, int]] = {}

    class Config:
        extra = "allow"
