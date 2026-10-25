"""Qt-free core of eXact: pixel buffers, PNG I/O, transforms, history, diffing.

Everything that touches authoritative pixel data lives here so it can be tested
headlessly and never depends on a GUI rendering buffer.
"""
