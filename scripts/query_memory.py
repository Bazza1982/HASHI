#!/usr/bin/env python3
"""
BGE-M3 semantic search over consolidated_memory.sqlite.
Usage: python3 query_memory.py "your query" [--top 10] [--agent sunny] [--since 2026-03-01]
"""

import argparse
import sqlite3
import struct
import sys
import os
import numpy as np

DB_PATH = "/home/lily/projects/hashi/workspaces/lily/consolidated_memory.sqlite"
MODEL_PATH = "/mnt/c/Users/thene/.cache/bge-m3-onnx-npu/bge-m3-int8.onnx"


def encode(text: str) -> np.ndarray:
    import onnxruntime as ort
    from transformers import AutoTokenizer

    tokenizer = AutoTokenizer.from_pretrained("BAAI/bge-m3", trust_remote_code=True)
    session = ort.InferenceSession(MODEL_PATH, providers=["CPUExecutionProvider"])
    inputs = tokenizer([text], padding=True, truncation=True, max_length=512, return_tensors="np")
    outputs = session.run(None, {
        "input_ids": inputs["input_ids"].astype(np.int64),
        "attention_mask": inputs["attention_mask"].astype(np.int64),
    })
    # Use outputs[1]: sentence-level pooled embedding (batch, 1024)
    # Must match consolidate_memory.py which also uses outputs[1]
    vec = outputs[1][0]
    vec = vec / (np.linalg.norm(vec) + 1e-9)
    return vec.astype(np.float32)


def blob_to_vec(blob) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("query", help="Search query")
    parser.add_argument("--top", type=int, default=10)
    parser.add_argument("--agent", default=None, help="Filter by agent_id")
    parser.add_argument("--since", default=None, help="Filter by date e.g. 2026-03-01")
    args = parser.parse_args()

    print(f"Encoding query with BGE-M3...", file=sys.stderr)
    q_vec = encode(args.query)

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()

    sql = "SELECT id, instance, agent_id, source_ts, domain, content, embedding FROM consolidated WHERE embedding IS NOT NULL"
    params = []
    if args.agent:
        sql += " AND agent_id = ?"
        params.append(args.agent)
    if args.since:
        sql += " AND source_ts >= ?"
        params.append(args.since)

    cur.execute(sql, params)
    rows = cur.fetchall()
    conn.close()

    print(f"Scoring {len(rows)} records...", file=sys.stderr)

    scored = []
    for row in rows:
        rid, instance, agent_id, ts, domain, content, emb_blob = row
        if not emb_blob:
            continue
        vec = blob_to_vec(emb_blob)
        score = float(np.dot(q_vec, vec))
        scored.append((score, ts, instance, agent_id, domain, content))

    scored.sort(reverse=True)

    print(f"\n=== Top {args.top} results for: \"{args.query}\" ===\n")
    for i, (score, ts, instance, agent_id, domain, content) in enumerate(scored[:args.top], 1):
        print(f"[{i}] score={score:.3f} | {ts[:10]} | {instance}/{agent_id} | {domain}")
        print(f"    {content[:300]}")
        print()


if __name__ == "__main__":
    main()
