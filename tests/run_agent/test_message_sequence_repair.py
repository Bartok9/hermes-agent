"""Tests for pre-API-call message-sequence repair.

Covers ``_repair_message_sequence`` and the extended
``_drop_trailing_empty_response_scaffolding`` behavior that rewinds past
orphan tool-result tails. Together these prevent the self-reinforcing empty-
response loop observed in session 20260507_044111_fa7e65, where a tool-result
followed directly by a user message produced silent empty responses from
providers (violating role alternation), which retriggered the empty-retry
recovery every turn.
"""

from run_agent import AIAgent


def _bare_agent():
    return AIAgent.__new__(AIAgent)


# ── _drop_trailing_empty_response_scaffolding ──────────────────────────────

def test_drop_scaffolding_rewinds_orphan_tool_tail():
    """When scaffolding is stripped, also rewind the orphan assistant+tool pair."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "assistant", "content": "(empty)",
         "_empty_terminal_sentinel": True},
    ]

    AIAgent._drop_trailing_empty_response_scaffolding(agent, messages)

    assert messages == [{"role": "user", "content": "task"}]


def test_drop_scaffolding_keeps_tail_when_no_scaffolding():
    """Mid-iteration tool results must NOT be rewound — only if scaffolding fires."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
    ]
    original = [dict(m) for m in messages]

    AIAgent._drop_trailing_empty_response_scaffolding(agent, messages)

    assert messages == original


def test_drop_scaffolding_handles_multiple_parallel_tool_results():
    """Parallel tool calls (one assistant → many tool results) all rewound together."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "task"},
        {"role": "assistant", "content": "",
         "tool_calls": [
             {"id": "t1", "type": "function",
              "function": {"name": "f", "arguments": "{}"}},
             {"id": "t2", "type": "function",
              "function": {"name": "g", "arguments": "{}"}},
         ]},
        {"role": "tool", "tool_call_id": "t1", "content": "out1"},
        {"role": "tool", "tool_call_id": "t2", "content": "out2"},
        {"role": "assistant", "content": "(empty)",
         "_empty_terminal_sentinel": True},
    ]

    AIAgent._drop_trailing_empty_response_scaffolding(agent, messages)

    assert messages == [{"role": "user", "content": "task"}]


# ── _repair_message_sequence ───────────────────────────────────────────────

def test_repair_merges_consecutive_user_messages():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "first"},
        {"role": "user", "content": "second"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 1
    assert len(messages) == 1
    assert messages[0]["role"] == "user"
    assert messages[0]["content"] == "first\n\nsecond"


def test_repair_preserves_user_content_when_one_side_empty():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": ""},
        {"role": "user", "content": "real message"},
    ]

    AIAgent._repair_message_sequence(agent, messages)

    assert messages == [{"role": "user", "content": "real message"}]


def test_repair_does_not_rewind_ongoing_dialog_tool_pair():
    """assistant(tool_calls) + tool + user is a VALID pattern (user redirect
    before the model gets its continuation turn). Repair must not touch it —
    only the flag-gated scaffolding strip rewinds, and only when the
    empty-recovery scaffolding was actually present.
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "Q1"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "f", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "out"},
        {"role": "user", "content": "Q2"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_drops_stray_tool_with_unknown_tool_call_id():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "hello"},
        {"role": "tool", "tool_call_id": "orphan", "content": "stray"},
        {"role": "user", "content": "real"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    assert all(m.get("role") != "tool" for m in messages)


def test_repair_leaves_valid_conversation_unchanged():
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "t1", "type": "function",
                         "function": {"name": "ls", "arguments": "{}"}}]},
        {"role": "tool", "tool_call_id": "t1", "content": "a.txt b.txt"},
        {"role": "assistant", "content": "Found 2 files"},
        {"role": "user", "content": "more"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == original


def test_repair_preserves_multimodal_user_content():
    """Multimodal (list) content must NOT be merged — risks mangling attachments."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": [{"type": "text", "text": "hi"},
                                     {"type": "image_url", "image_url": {"url": "..."}}]},
        {"role": "user", "content": "follow-up"},
    ]

    AIAgent._repair_message_sequence(agent, messages)

    # The multimodal user message stays as a distinct message — no merge
    assert len(messages) == 2
    assert isinstance(messages[0]["content"], list)


def test_repair_empty_messages_returns_zero():
    agent = _bare_agent()
    messages = []

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert messages == []


def test_repair_preserves_system_messages():
    agent = _bare_agent()
    messages = [
        {"role": "system", "content": "You are..."},
        {"role": "user", "content": "hi"},
    ]
    original = [dict(m) for m in messages]

    AIAgent._repair_message_sequence(agent, messages)

    assert messages == original


# ── Pass 3: merge consecutive assistant tool_calls messages (issue #29148) ─

def test_repair_merges_two_consecutive_assistant_tool_calls():
    """Two adjacent assistant-with-tool_calls messages collapse into one.

    DeepSeek v4 and other strict OpenAI-compatible providers reject a
    message history where parallel tool calls appear as separate assistant
    turns:
        assistant(tool_calls=[A]) → assistant(tool_calls=[B]) → tool(A) → tool(B)
    The repair must produce:
        assistant(tool_calls=[A, B]) → tool(A) → tool(B)
    """
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_A", "type": "function",
                            "function": {"name": "session_search", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "call_B", "type": "function",
                            "function": {"name": "search_files", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "result A"},
        {"role": "tool", "tool_call_id": "call_B", "content": "result B"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 1
    # Only one assistant message should remain
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    merged_calls = assistant_msgs[0]["tool_calls"]
    assert len(merged_calls) == 2
    call_ids = {tc["id"] for tc in merged_calls}
    assert call_ids == {"call_A", "call_B"}
    # Tool results still present and in correct order
    tool_msgs = [m for m in messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2


def test_repair_merges_three_consecutive_assistant_tool_calls():
    """Three adjacent assistant-with-tool_calls turns all collapse into one."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run three"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c1", "type": "function",
                            "function": {"name": "tool_x", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c2", "type": "function",
                            "function": {"name": "tool_y", "arguments": "{}"}}],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "c3", "type": "function",
                            "function": {"name": "tool_z", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "c1", "content": "r1"},
        {"role": "tool", "tool_call_id": "c2", "content": "r2"},
        {"role": "tool", "tool_call_id": "c3", "content": "r3"},
    ]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs >= 2
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 1
    assert len(assistant_msgs[0]["tool_calls"]) == 3


def test_repair_does_not_merge_text_only_assistants():
    """Plain-text assistant messages must NOT be merged (no tool_calls)."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "First thought"},
        {"role": "assistant", "content": "Second thought"},
    ]

    AIAgent._repair_message_sequence(agent, messages)

    # Both text assistant messages should still be separate
    assistant_msgs = [m for m in messages if m.get("role") == "assistant"]
    assert len(assistant_msgs) == 2


def test_repair_does_not_merge_tool_calls_separated_by_tool_result():
    """Two assistant-with-tool_calls separated by a tool result are NOT merged."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "go"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t1", "type": "function",
                            "function": {"name": "f", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t1", "content": "done"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "t2", "type": "function",
                            "function": {"name": "g", "arguments": "{}"}}],
        },
        {"role": "tool", "tool_call_id": "t2", "content": "done2"},
    ]
    original_assistant_count = sum(1 for m in messages if m.get("role") == "assistant")

    AIAgent._repair_message_sequence(agent, messages)

    assert sum(1 for m in messages if m.get("role") == "assistant") == original_assistant_count


def test_repair_preserves_message_count_for_valid_parallel_format():
    """A correctly-formatted single assistant with multiple tool_calls is unchanged."""
    agent = _bare_agent()
    messages = [
        {"role": "user", "content": "run both"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {"id": "call_A", "type": "function",
                 "function": {"name": "session_search", "arguments": "{}"}},
                {"id": "call_B", "type": "function",
                 "function": {"name": "search_files", "arguments": "{}"}},
            ],
        },
        {"role": "tool", "tool_call_id": "call_A", "content": "result A"},
        {"role": "tool", "tool_call_id": "call_B", "content": "result B"},
    ]
    original = [dict(m) for m in messages]

    repairs = AIAgent._repair_message_sequence(agent, messages)

    assert repairs == 0
    assert len(messages) == len(original)
