"""DataTable with optional Vim-style keyboard navigation."""

from textual.binding import Binding
from textual.widgets import DataTable


class VimDataTable(DataTable):
    """Navigate table rows using arrows or Vim keys."""

    BINDINGS = [
        Binding("j", "cursor_down", "Next row", show=False),
        Binding("k", "cursor_up", "Previous row", show=False),
    ]
