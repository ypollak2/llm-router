"""Language adapters. Python first, because it is what llm-router is.

Each adapter answers one question: given the exact bytes of a file, what does
it define and what does it reference? Deterministically, with no model in the
path — a claim about what a file defines has to be one the file can be made to
prove, and `ast` can prove it where a regex can only suggest it.
"""
