"""codeagent -- the server side of M2C (the `m2c` terminal client's brain).

Named `codeagent`, not `m2c`, so it can never clash with the top-level `m2c/`
client package when both sit in one dev environment (build plan O5).

Patch S04 creates this package with the device socket (ws.py, connections.py).
Later patches add modules beside them (the model adapter, the run manager, the
tool broker); this file stays empty of imports on purpose, so importing a
sibling never drags in the socket or its dependencies.
"""
