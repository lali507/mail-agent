"""Email reply agent with a daily human approval round (LangGraph)."""

# Verify TLS with the OS certificate store (needed behind filters such as NetFree,
# whose root CA is installed in Windows but not in Python's bundled CA list).
try:
    import truststore

    truststore.inject_into_ssl()
except ImportError:  # optional dependency
    pass
