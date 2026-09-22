"""Capture + parse a `claude -p --output-format stream-json --include-partial-messages`
stream. Reads the event stream on stdin and writes two files:
  argv[1] raw.jsonl  -- the raw stream-json events, one per line (the token trace)
  argv[2] trace.txt  -- a parsed timeline (also echoed to stdout), columns:
                          <raw.jsonl line>   <elapsed s>   <event>
The point: a tool call's NAME appears at `content_block_start`, before its arguments finish
streaming (`input_json_delta` chunks). The `lead` figure is how much earlier the name was
known than the completed input. The `L<n>` column cross-references the raw event in raw.jsonl.
"""
import sys, time, json

raw = open(sys.argv[1], "w")
out = open(sys.argv[2], "w")

def emit(text, lno=None):
    s = text if lno is None else ("%-5s %s" % ("L%d" % lno, text))
    print(s)
    out.write(s + "\n"); out.flush()

t0 = time.time()
ln = 0           # current line number in raw.jsonl
cur = None       # (tool_name, start_dt)
emit("# tool-call token trace")
emit("# claude -p --output-format stream-json --include-partial-messages --verbose --model sonnet")
emit("# columns:  <raw.jsonl line>   <elapsed s>   <event>")
emit("")

for line in iter(sys.stdin.readline, ""):
    s = line.rstrip("\n")
    if not s.strip():
        continue
    raw.write(s + "\n"); raw.flush()
    ln += 1
    try:
        o = json.loads(s)
    except Exception:
        continue
    dt = time.time() - t0
    typ = o.get("type")
    if typ == "stream_event":
        ev = o.get("event", {}) or {}
        et = ev.get("type")
        if et == "content_block_start":
            cb = ev.get("content_block", {}) or {}
            if cb.get("type") == "tool_use":
                cur = (cb.get("name"), dt)
                emit("%7.2f  >> TOOL_USE START   name=%r   <-- name known here, no args yet" % (dt, cb.get("name")), ln)
            elif cb.get("type") == "text":
                emit("%7.2f  .. assistant text block begins" % dt, ln)
        elif et == "content_block_delta":
            d = ev.get("delta", {}) or {}
            if d.get("type") == "input_json_delta":
                emit("%7.2f       args-delta   %r" % (dt, (d.get("partial_json") or "")[:60]), ln)
        elif et == "content_block_stop":
            if cur:
                name, st = cur
                emit("%7.2f  -- tool input COMPLETE   name=%r   lead=%.2fs" % (dt, name, dt - st), ln)
                cur = None
            else:
                emit("%7.2f  -- block complete" % dt, ln)
    elif typ == "assistant":
        for c in ((o.get("message") or {}).get("content") or []):
            if isinstance(c, dict) and c.get("type") == "tool_use":
                inp = c.get("input") or {}
                prev = {k: (str(v)[:40] + ("..." if len(str(v)) > 40 else "")) for k, v in inp.items()}
                emit("%7.2f  [resolved tool_use]  name=%r  input=%s" % (dt, c.get("name"), prev), ln)
    elif typ == "user":
        for c in ((o.get("message") or {}).get("content") or []):
            if isinstance(c, dict) and c.get("type") == "tool_result":
                emit("%7.2f  <- tool_result returned (id=%s)" % (dt, str(c.get("tool_use_id"))[:18]), ln)
    elif typ == "result":
        emit("")
        emit("%7.2f  RESULT  cost=$%s  turns=%s" % (dt, o.get("total_cost_usd"), o.get("num_turns")), ln)

raw.close(); out.close()
