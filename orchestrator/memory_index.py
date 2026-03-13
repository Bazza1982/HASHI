import sqlite3
from pathlib import Path
import logging
from typing import List, Dict, Any

logger = logging.getLogger("BridgeU.MemoryIndex")

class MemoryIndex:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._init_db()

    def _init_db(self):
        with sqlite3.connect(self.db_path) as conn:
            # Create a virtual table using FTS5
            conn.execute('''
                CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
                    file_path,
                    header_path,
                    content,
                    source_type,
                    tokenize="porter"
                )
            ''')
            # Metadata table to keep track of indexed files and modified times
            conn.execute('''
                CREATE TABLE IF NOT EXISTS indexed_files (
                    file_path TEXT PRIMARY KEY,
                    mtime REAL
                )
            ''')
            conn.commit()

    def _chunk_text(self, text: str, max_size: int = 1500, overlap: int = 200) -> List[str]:
        # Basic chunking by paragraphs, fallback to size
        paragraphs = text.split('\n\n')
        chunks = []
        current_chunk = ""
        
        for p in paragraphs:
            if len(current_chunk) + len(p) < max_size:
                current_chunk += p + "\n\n"
            else:
                if current_chunk:
                    chunks.append(current_chunk.strip())
                if len(p) > max_size:
                    # Fallback to chunking string by size
                    for i in range(0, len(p), max_size - overlap):
                        chunks.append(p[i:i + max_size])
                    current_chunk = ""
                else:
                    current_chunk = p + "\n\n"
        if current_chunk:
            chunks.append(current_chunk.strip())
            
        return chunks

    def index_directory(self, directory: Path):
        """Indexes all markdown files in the given directory."""
        if not directory.exists():
            return
            
        with sqlite3.connect(self.db_path) as conn:
            for md_file in directory.rglob('*.md'):
                mtime = md_file.stat().st_mtime
                
                # Check if we need to reindex
                cur = conn.cursor()
                cur.execute('SELECT mtime FROM indexed_files WHERE file_path = ?', (str(md_file),))
                row = cur.fetchone()
                
                if row and row[0] >= mtime:
                    continue # Already up to date
                
                # Reindex
                # First delete old entries
                conn.execute('DELETE FROM memory_fts WHERE file_path = ?', (str(md_file),))
                
                # Read and chunk new content
                try:
                    text = md_file.read_text(encoding='utf-8')
                    chunks = self._chunk_text(text)
                    source_type = md_file.parent.name if md_file.parent.name != directory.name else 'root'
                    
                    for i, chunk in enumerate(chunks):
                        conn.execute('''
                            INSERT INTO memory_fts (file_path, header_path, content, source_type)
                            VALUES (?, ?, ?, ?)
                        ''', (str(md_file), f"chunk-{i}", chunk, source_type))
                        
                    conn.execute('''
                        INSERT OR REPLACE INTO indexed_files (file_path, mtime)
                        VALUES (?, ?)
                    ''', (str(md_file), mtime))
                    
                    logger.info(f"Indexed {md_file.name} ({len(chunks)} chunks)")
                except Exception as e:
                    logger.error(f"Failed to index {md_file}: {e}")
                    
            conn.commit()

    def search(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        results = []
        try:
            with sqlite3.connect(self.db_path) as conn:
                conn.row_factory = sqlite3.Row
                # Escape query for FTS to avoid syntax errors
                safe_query = query.replace('"', '""')
                cur = conn.execute('''
                    SELECT file_path, header_path, content, source_type, rank
                    FROM memory_fts
                    WHERE memory_fts MATCH ?
                    ORDER BY rank
                    LIMIT ?
                ''', (f'"{safe_query}"', limit))
                
                for row in cur:
                    results.append(dict(row))
        except Exception as e:
            logger.error(f"Search failed for query '{query}': {e}")
            
        return results
