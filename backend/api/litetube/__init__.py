# Litetube: package marker.
# All FastAPI surfaces are hosted in this package; uvicorn target is "litetube.main:app".

# Canonical version string — single source of truth. Bump per SemVer.
# Visible in /health JSON, /docs (OpenAPI), and HTML footers (injected as
# the {{VERSION}} placeholder by main.py on each render).
__version__ = "0.2.0"
