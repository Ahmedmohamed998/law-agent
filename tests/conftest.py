"""
Tests that run without Postgres, AWS or the Chroma index.

chromadb is stubbed before anything imports it: the retriever imports it at
module level, and the API module imports the retriever, but nothing under test
here builds one. Installing the real package would add a native build and a
few hundred megabytes to prove nothing.
"""

import sys
import types

_chromadb = types.ModuleType("chromadb")
_chromadb.Collection = type("Collection", (), {})  # used as a type annotation
sys.modules.setdefault("chromadb", _chromadb)
