"""Canonical text shared by memory-item retrieval and embedding generation."""

MEMORY_ITEM_TEXT_SQL = """concat_ws(' ', mi.kind,
    nullif(mi.content->>'item_key', ''),
    nullif(mi.content->>'title', ''),
    nullif(mi.content->>'summary', ''),
    nullif(mi.content->>'text', ''),
    nullif(mi.content->>'content', ''),
    nullif(mi.content->>'situation', ''),
    nullif(mi.content->>'lesson', ''),
    nullif(mi.content->>'rationale', ''),
    nullif(mi.content->>'reason', ''),
    nullif(mi.content->>'cause', ''),
    nullif(mi.content->>'effect', ''),
    nullif(mi.content->>'outcome', ''),
    nullif(mi.content->>'result', ''),
    nullif(mi.content->>'recommendation', ''),
    nullif(mi.content->>'action', ''),
    nullif(mi.content->>'description', ''))"""
