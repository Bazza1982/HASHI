"""
Veritas — Academic PDF ingestion pipeline for Obsidian vault + RAG.

Registers callable adapters for use with Nagare's RoutingStepHandler:
  - mineru-extractor
  - knowledge-block-assembler
  - vault-writer
"""

from veritas.adapters.knowledge_block_assembler import knowledge_block_assembler
from veritas.adapters.mineru_extractor import mineru_extractor
from veritas.adapters.vault_writer import vault_writer

CALLABLE_REGISTRY: dict = {
    "mineru-extractor": mineru_extractor,
    "knowledge-block-assembler": knowledge_block_assembler,
    "vault-writer": vault_writer,
}

__all__ = ["CALLABLE_REGISTRY", "mineru_extractor", "knowledge_block_assembler", "vault_writer"]
