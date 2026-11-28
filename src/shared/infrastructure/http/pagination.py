"""Pagination limits shared by every REST router (backend #255).

Page-number pagination has no natural ceiling: `page=1000000` turns into a huge
OFFSET and makes the upstream module scan the whole table. The routers validate
the query parameter against `MAX_PAGE` *before* the handler runs, so an
out-of-range value answers `400 VALIDACION` without touching the database, the
modules or the tool layer.
"""

MAX_PAGE = 1000
