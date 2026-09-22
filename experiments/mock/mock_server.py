"""Anthropic API Compatible Mock Server for Claude Code Trace Replay.

This server replays pre-recorded Claude Code trace files (trace.jsonl) as if
they were live Anthropic API responses. It serves as a mock endpoint so that
Claude Code can be run against recorded traces without making real API calls.

It makes some assumptions based off our observations on the corpus. Because Claude Code
is closed source, this assumptions could break for future versions.

At the time of writing, we are using claude version 2.1.161

Assumptions & Observations
==========================================

1. **Trace format**: Each trace is a JSONL file. Each line is a JSON object
   with fields: uuid, parentUuid, isSidechain, type ("user"/"assistant"/
   "summary"), timestamp, message (with role, id, content), sessionId, etc.

2. **Main chain vs sidechains**: Claude Code can spawn subagents via the
   `Task` tool. The main conversation is `isSidechain=false`; subagent
   conversations are `isSidechain=true`. Both share the same API key
   (leafUuid), so all requests from a single trace hit this server with
   the same authorization token. This matters because we are currently using the
   leafUuid to serve as the claude code API key to identify the session, this
   is crucial for allowing us to benchmark our scheduler to support a multi-tenant
   claude code setup.

3. **Multiple subagents**: A trace may contain 0, 1, or more subagent
   invocations. From corpus analysis (59 tasks):
   - 45 tasks: no subagents (main chain only)
   - 13 tasks: 1 subagent invocation
   - 1 task:  2 sequential subagent invocations
   Subagents appear as contiguous blocks of `isSidechain=true` entries
   in the trace, separated by main-chain entries.
"""

from pathlib import Path
import asyncio
import json
import uuid
import os

from fastapi import FastAPI, HTTPException, Header, UploadFile, File
from fastapi.responses import StreamingResponse, JSONResponse
from datetime import datetime
import uvicorn

from experiments.analysis.utils import parse_timestamp

from experiments.mock.mock_types import (
    CreateMessageRequest,
    ReplaySession,
    ChainEvents,
)

PRODUCTION_MODE = os.environ.get("PRODUCTION_MODE", "true").lower() == "true"
FAST_FORWARD_MODE = os.environ.get("FAST_FORWARD_MODE", "false").lower() == "true"

print("=" * 30)
print(f"\tPRODUCTION_MODE: {PRODUCTION_MODE}")
print(f"\tFAST_FORWARD_MODE: {FAST_FORWARD_MODE}")
print("=" * 30)


_builtin_print = print


def print(*args, **kwargs):
    if not PRODUCTION_MODE:
        _builtin_print(*args, **kwargs)


app = FastAPI(title="Anthropic API Compatible Mock Server")
REPLAY: dict[str, ReplaySession] = {}

# session_id -> {
#     "last_served_chain": str,
#     "last_served_turn": int,
#     "sidechains_started": int,
# }
SESSION_STATE: dict[str, dict] = {}
counter = 0


# ── SSE helper and emulating LLM streaming ──
def _format_sse_frame(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _extract_raw_usage(event: dict) -> dict:
    usage = event.get("usage", {})
    if not usage:
        usage = {"input_tokens": 100, "output_tokens": 50}
    return usage


async def _stream_turn(event: dict, model: str, pre_delay_ms: float):
    """Yield assistant messages to mock anthropic SSE client expectation"""
    content_blocks = event["message"]["content"]
    usage = _extract_raw_usage(event)
    msg_id = event.get("uuid")

    has_tool_use = any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content_blocks
    )
    stop_reason = "tool_use" if has_tool_use else "end_turn"

    if not FAST_FORWARD_MODE and pre_delay_ms > 0:
        await asyncio.sleep(pre_delay_ms / 1000.0)

    start_time = asyncio.get_event_loop().time()
    block_delays = event.get("block_delays_ms", [])

    yield _format_sse_frame(
        "message_start",
        {
            "type": "message_start",
            "message": {
                "id": msg_id,
                "type": "message",
                "role": "assistant",
                "content": [],
                "model": model,
                "stop_reason": None,
                "stop_sequence": None,
                "usage": usage,
            },
        },
    )

    # Claude Code client expects a ping event here
    yield _format_sse_frame("ping", {"type": "ping"})

    for idx, block in enumerate(content_blocks):
        if not isinstance(block, dict):
            continue

        target_delay = block_delays[idx] / 1000.0 if idx < len(block_delays) else 0.0
        elapsed = asyncio.get_event_loop().time() - start_time
        sleep_needed = target_delay - elapsed
        if not FAST_FORWARD_MODE and sleep_needed > 0:
            await asyncio.sleep(sleep_needed)

        block_type = block.get("type", "text")

        if block_type == "text":
            text = block.get("text", "")
            yield _format_sse_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            chunk_size = 12
            for i in range(0, max(len(text), 1), chunk_size):
                yield _format_sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "text_delta",
                            "text": text[i : i + chunk_size],
                        },
                    },
                )
                if not FAST_FORWARD_MODE:
                    await asyncio.sleep(0.002)
            yield _format_sse_frame(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": idx,
                },
            )

        elif block_type == "tool_use":
            tool_id = block.get("id", f"toolu_{uuid.uuid4().hex[:24]}")
            yield _format_sse_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {
                        "type": "tool_use",
                        "id": tool_id,
                        "name": block.get("name", ""),
                        "input": {},
                    },
                },
            )
            input_json = json.dumps(block.get("input", {}))
            chunk_size = 30
            for i in range(0, max(len(input_json), 1), chunk_size):
                yield _format_sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": input_json[i : i + chunk_size],
                        },
                    },
                )
                if not FAST_FORWARD_MODE:
                    await asyncio.sleep(0.002)
            yield _format_sse_frame(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": idx,
                },
            )

        elif block_type == "thinking":
            yield _format_sse_frame(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": idx,
                    "content_block": {"type": "thinking", "thinking": ""},
                },
            )
            thinking_text = block.get("thinking", "")
            chunk_size = 20
            for i in range(0, max(len(thinking_text), 1), chunk_size):
                yield _format_sse_frame(
                    "content_block_delta",
                    {
                        "type": "content_block_delta",
                        "index": idx,
                        "delta": {
                            "type": "thinking_delta",
                            "thinking": thinking_text[i : i + chunk_size],
                        },
                    },
                )
                if not FAST_FORWARD_MODE:
                    await asyncio.sleep(0.002)
            yield _format_sse_frame(
                "content_block_stop",
                {
                    "type": "content_block_stop",
                    "index": idx,
                },
            )

    yield _format_sse_frame(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason, "stop_sequence": None},
            "usage": {"output_tokens": usage.get("output_tokens", 50)},
        },
    )

    yield _format_sse_frame("message_stop", {"type": "message_stop"})


def _resolve_turn(
    session_id: str, request: CreateMessageRequest, replay: ReplaySession
) -> tuple[dict, int, str]:
    """
    Route an incoming request to the correct chain using tool_use_id mapping.
    """
    state = SESSION_STATE.get(session_id)
    if state is None:
        state = {
            "last_served_chain": "main",
            "last_served_turn": 0,
            "sidechains_started": 0,
        }
        SESSION_STATE[session_id] = state

    # Extract the last tool_result ID from the user messages
    last_tool_id = None
    for msg in reversed(request.messages):
        if getattr(msg, "role", None) == "user" and isinstance(
            getattr(msg, "content", None), list
        ):
            for block in msg.content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    last_tool_id = block.get("tool_use_id")
                    if last_tool_id:
                        break
        if last_tool_id:
            break

    if last_tool_id and last_tool_id in replay.tool_to_next_event:
        chain_label, turn_index = replay.tool_to_next_event[last_tool_id]
        state["last_served_chain"] = chain_label
        state["last_served_turn"] = turn_index

        if chain_label == "main":
            event = replay.main_chain.events[turn_index]
        else:
            side_idx = int(chain_label.split("_")[1])
            event = replay.side_chains[side_idx].events[turn_index]

        print(
            f"  [lookup] tool_use_id={last_tool_id[:15]}... -> {chain_label} turn {turn_index}"
        )
        return event, turn_index, chain_label

    # Fallbacks (no tool results found)
    has_assistant = any(
        getattr(m, "role", None) == "assistant" for m in request.messages
    )

    if not has_assistant:
        if state["last_served_turn"] == 0 and state["last_served_chain"] == "main":
            # First request ever
            print("  [start] main chain turn 0")
            return replay.main_chain.events[0], 0, "main"
        else:
            # Subagent launch
            sc_idx = state["sidechains_started"]
            if sc_idx < len(replay.side_chains):
                state["sidechains_started"] += 1
                state["last_served_chain"] = f"side_{sc_idx}"
                state["last_served_turn"] = 0
                print(f"  [start] sidechain {sc_idx} turn 0")
                return replay.side_chains[sc_idx].events[0], 0, f"side_{sc_idx}"
            else:
                print("  [WARNING] all sidechains exhausted, falling back")

    # Final fallback: advance whatever was last served
    chain_label = state["last_served_chain"]
    turn_index = state["last_served_turn"] + 1

    if chain_label == "main":
        chain = replay.main_chain
    else:
        side_idx = int(chain_label.split("_")[1])
        chain = replay.side_chains[side_idx]

    turn_index = min(turn_index, len(chain.events) - 1)
    state["last_served_turn"] = turn_index

    print(
        f"  [fallback] no tool result matched, advancing {chain_label} to {turn_index}"
    )
    return chain.events[turn_index], turn_index, chain_label


# ── Anthropic API ──
@app.post("/v1/messages")
async def create_message(
    request: CreateMessageRequest,
    authorization: str | None = Header(None),
):
    global counter

    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")

    parent_uuid = authorization.removeprefix("Bearer ").strip()
    replay: ReplaySession | None = REPLAY.get(parent_uuid)
    if not replay:
        raise HTTPException(
            status_code=400,
            detail=f"No replay found for API key: {parent_uuid}",
        )

    if not replay.main_chain.events:
        raise HTTPException(status_code=400, detail="No events in main chain.")

    msg_count = len(request.messages)
    event, turn_index, chain_label = _resolve_turn(parent_uuid, request, replay)

    now = datetime.now()
    formatted_time = now.strftime("%B %d %Y %H:%M")
    print(
        f"{formatted_time} [turn {turn_index}] chain={chain_label}  session={parent_uuid[:15]}…  "
        f"msg_count={msg_count}  stream={request.stream} msg_id={event['message']['id']} "
    )

    pre_delay_ms = event.get("pre_delay_ms", 0)

    if request.stream:
        if not PRODUCTION_MODE:
            with open(f"log_{counter}", "w") as f:
                messages_list = [
                    m.model_dump() if hasattr(m, "model_dump") else m.dict()
                    for m in request.messages
                ]
                json.dump(messages_list, f, indent=2)
            counter += 1
        return StreamingResponse(
            _stream_turn(event, request.model, pre_delay_ms),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )
    else:
        content = event["message"]["content"]
        has_tool_use = any(
            isinstance(b, dict) and b.get("type") == "tool_use" for b in content
        )
        return JSONResponse(
            content={
                "id": event.get("uuid"),
                "type": "message",
                "role": "assistant",
                "content": content,
                "model": request.model,
                "stop_reason": "tool_use" if has_tool_use else "end_turn",
                "stop_sequence": None,
                "usage": _extract_raw_usage(event),
            }
        )


@app.get("/v1/models")
async def list_models():
    return {
        "data": [
            {
                "id": "sample",
                "object": "model",
                "created": int(datetime.now().timestamp()),
                "owned_by": "local",
            }
        ],
        "object": "list",
    }


@app.get("/health")
async def health_check():
    return {"status": "healthy", "model": "sample"}


# --- Mock Server API ---
@app.post("/v1/replays")
async def create_replay(trace: UploadFile = File(...)):
    try:
        lines = []
        for line in trace.file:
            stripped = line.strip() if isinstance(line, str) else line.decode().strip()
            if stripped:
                lines.append(json.loads(stripped))
        if not lines:
            raise HTTPException(status_code=400, detail="Empty trace file")

        if "leafUuid" in lines[0]:
            identifier = lines[0]["leafUuid"]
        elif "sessionId" in lines[0]:
            identifier = lines[0]["sessionId"]
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Missing leafUuid or sessionId in trace {trace.filename}",
            )

        # ── Separate main-chain and sidechain entries ──
        # Each contiguous block of isSidechain=true entries forms one
        # sidechain (one subagent invocation).
        main_entries = []
        side_chain_groups: list[list[dict]] = []
        current_side_group: list[dict] = []
        in_sidechain = False

        for obj in lines:
            if not isinstance(obj, dict):
                continue
            is_side = obj.get("isSidechain", False)
            if is_side:
                current_side_group.append(obj)
                in_sidechain = True
            else:
                if in_sidechain and current_side_group:
                    side_chain_groups.append(current_side_group)
                    current_side_group = []
                    in_sidechain = False
                main_entries.append(obj)

        if current_side_group:
            side_chain_groups.append(current_side_group)

        def _extract_events(entries: list[dict]) -> list[dict]:
            """Parse a list of trace entries into deduplicated assistant events."""
            events = []
            id_to_event: dict[str, dict] = {}
            prev_ts = None

            for obj in entries:
                ts = parse_timestamp(obj.get("timestamp"))
                if ts is None:
                    continue

                msg = obj.get("message")
                if not isinstance(msg, dict) or msg.get("role") != "assistant":
                    if ts is not None:
                        prev_ts = ts
                    continue

                msg_id = msg.get("id")
                if not msg_id:
                    if ts is not None:
                        prev_ts = ts
                    continue

                content = msg.get("content", [])
                content_list = content if isinstance(content, list) else [content]
                usage = msg.get("usage", {})

                if msg_id not in id_to_event:
                    pre_delay_ms = 0
                    if ts is not None and prev_ts is not None:
                        pre_delay_ms = max(0, (ts - prev_ts) * 1000)

                    event = {
                        "message": {
                            "id": msg_id,
                            "content": list(content_list),
                        },
                        "usage": usage,
                        "pre_delay_ms": pre_delay_ms,
                        "parentUuid": obj.get("parentUuid", None),
                        "uuid": obj["uuid"],
                        "isSidechain": obj.get("isSidechain", False),
                        "block_delays_ms": [0.0] * len(content_list),
                        "start_ts": ts,
                    }
                    id_to_event[msg_id] = event
                    events.append(event)
                else:
                    existing = id_to_event[msg_id]
                    prev_len = len(existing["message"]["content"])
                    existing["message"]["content"].extend(content_list)

                    new_len = len(existing["message"]["content"])
                    for _ in range(prev_len, new_len):
                        delay = 0.0
                        if ts is not None and existing.get("start_ts") is not None:
                            delay = max(0.0, (ts - existing["start_ts"]) * 1000)
                        existing["block_delays_ms"].append(delay)

                    if usage:
                        existing["usage"] = usage

                if ts is not None:
                    prev_ts = ts

            return events

        main_chain_events = _extract_events(main_entries)
        side_chains = [
            ChainEvents(events=_extract_events(group)) for group in side_chain_groups
        ]

        tool_to_next_event = {}

        for i, event in enumerate(main_chain_events):
            content = event.get("message", {}).get("content", [])
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_id = block.get("id")
                    if tool_id and i + 1 < len(main_chain_events):
                        tool_to_next_event[tool_id] = ("main", i + 1)

        for j, sc in enumerate(side_chains):
            for i, event in enumerate(sc.events):
                content = event.get("message", {}).get("content", [])
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use":
                        tool_id = block.get("id")
                        if tool_id and i + 1 < len(sc.events):
                            tool_to_next_event[tool_id] = (f"side_{j}", i + 1)

    except Exception as e:
        print(f"Error processing trace {trace.filename}: {e}")
        raise HTTPException(status_code=500, detail=f"Failed to process trace: {e}")

    main_chain = ChainEvents(events=main_chain_events)
    is_update = identifier in REPLAY
    REPLAY[identifier] = ReplaySession(
        identifier=identifier,
        main_chain=main_chain,
        side_chains=side_chains,
        tool_to_next_event=tool_to_next_event,
    )
    if not PRODUCTION_MODE:
        dump_chains(REPLAY[identifier], Path("traces_parsed"), trace.filename)

    SESSION_STATE.pop(identifier, None)

    side_info = ""
    if side_chains:
        side_turns = [len(sc.events) for sc in side_chains]
        side_info = f"  side_chains={len(side_chains)} (turns={side_turns})"

    if is_update:
        print(
            f"[replay] id={identifier} - Replay updated  "
            f"main_turns={len(main_chain_events)}{side_info}"
        )
    else:
        print(
            f"[replay] id={identifier}  main_turns={len(main_chain_events)}{side_info}"
        )
    return {
        "identifier": identifier,
        "main_turns": len(main_chain_events),
        "side_chains": len(side_chains),
        "message": (
            f"Loaded {len(main_chain_events)} main-chain turns "
            f"and {len(side_chains)} sidechain(s) from trace"
        ),
    }


def dump_chains(replay: ReplaySession, output_dir: Path, trace_name: str):
    """
    Produces:
        {output_dir}/{trace_name}_main_chain.json
        {output_dir}/{trace_name}_side_chain_0.json
        {output_dir}/{trace_name}_side_chain_1.json
        ...
    """
    output_dir.mkdir(parents=True, exist_ok=True)

    main_path = output_dir / f"{trace_name}_main_chain.json"
    with open(main_path, "w") as f:
        json.dump(replay.main_chain.events, f, indent=2)
    print(
        f"[dump] Wrote {len(replay.main_chain.events)} main-chain events -> {main_path}"
    )

    for i, sc in enumerate(replay.side_chains):
        side_path = output_dir / f"{trace_name}_side_chain_{i}.json"
        with open(side_path, "w") as f:
            json.dump(sc.events, f, indent=2)
        print(f"[dump] Wrote {len(sc.events)} sidechain-{i} events -> {side_path}")


# @app.get("/ready")
# async def health():
#     return {"status": "ready"}


def run_server(port=8000):
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        log_level="warning" if PRODUCTION_MODE else "info",
    )


if __name__ == "__main__":
    run_server()
