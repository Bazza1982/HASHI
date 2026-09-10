"""Frontend-owned terminal palettes shared by Textual CSS and Rich renderers."""
from __future__ import annotations

from rich.markdown import Markdown
from rich.theme import Theme as RichTheme
from textual.theme import Theme
from textual.widgets import RichLog

# background, surface, panel, text, border, primary, secondary, muted,
# accent, selection, success, error, warning; all surfaces use the same roles.
ROLES = ('background', 'surface', 'panel', 'text', 'border', 'primary',
         'secondary', 'muted', 'accent', 'selection', 'success', 'error', 'warning')
PALETTES = {
    'retro': ('#050b12','#091722','#101d28','#dff6ff','#2a5b82','#71b7ff','#9be7ff','#9fb3c8','#63ffd9','#ffdf6b','#c7ff8a','#ff7a7a','#ffd479'),
    'apple2': ('#001006','#001508','#00240e','#73ff9e','#36b461','#51ff88','#91ffb0','#6ac989','#3dff79','#92ffb0','#a2ffc0','#d0ffda','#bcffd0'),
    'nintendo': ('#fff1d1','#fff8e8','#f0d6ac','#241910','#99291f','#9a201b','#713628','#6d5641','#ac2119','#ffc05c','#236329','#b21b18','#784800'),
    'win32': ('#c0c0c0','#eeeeee','#d4d4d4','#101010','#666666','#000080','#173e70','#505050','#0000a0','#80b8ef','#126126','#b00020','#765000'),
    'atm': ('#031a32','#072541','#123551','#e3f1ff','#7a9dbb','#a7cff1','#bed3e5','#a3b8cc','#d2e8ff','#a7cff1','#a6e2bd','#ffada6','#ffe0a3'),
}


def register_themes(app):
    for name, values in PALETTES.items():
        p = dict(zip(ROLES, values))
        app.register_theme(Theme(
            name='hashi-' + name, primary=p['primary'], secondary=p['secondary'],
            accent=p['accent'], foreground=p['text'], background=p['background'],
            surface=p['surface'], panel=p['panel'], success=p['success'],
            error=p['error'], warning=p['warning'], dark=name not in {'nintendo','win32'},
            variables={**{'hashi-' + role: value for role, value in p.items()},
                       'hashi-selection-text':p['text'] if name in {'nintendo','win32'} else p['background']},
        ))


def apply_rich_theme(app, name):
    p = dict(zip(ROLES, PALETTES[name]))
    styles = {'hashi.' + role: value for role, value in p.items()}
    styles.update({modifier + ' hashi.' + role: modifier + ' ' + value
                   for modifier in ('bold', 'dim', 'italic', 'bold italic', 'underline')
                   for role, value in p.items()})
    styles.update({'markdown.code': p['primary'], 'markdown.link': 'underline ' + p['primary'],
                   'markdown.link_url': p['primary'], 'markdown.h1': 'bold ' + p['accent'],
                   'markdown.h2': 'bold ' + p['accent'], 'repr.number': p['primary'],
                   'repr.str': p['success'], 'yellow': p['warning'], 'red': p['error'],
                   'green': p['success'], 'cyan': p['secondary'], 'blue': p['primary']})
    if getattr(app, '_hashi_rich_theme_installed', False):
        app.console.pop_theme()
    app.console.push_theme(RichTheme(styles))
    app._hashi_rich_theme_installed = True


class ThemedMarkdown(Markdown):
    def __rich_console__(self, console, options):
        color = console.get_style('hashi.text', default='white').color
        rgb = color.get_truecolor() if color else (255, 255, 255)
        self.code_theme = 'monokai' if sum(rgb) > 384 else 'friendly'
        yield from super().__rich_console__(console, options)


class ThemedLog(RichLog):
    """Retain the visible renderable projection so theme changes recolor old text."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._theme_entries = []
        self._theme_replaying = False

    def write(self, content, *args, **kwargs):
        # RichLog replays deferred writes when its first layout determines size.
        if self._size_known and not self._theme_replaying:
            self._theme_entries.append((content, args, dict(kwargs)))
        result = super().write(content, *args, **kwargs)
        if self.max_lines and len(self._theme_entries) > self.max_lines:
            del self._theme_entries[:-self.max_lines]
        return result

    def clear(self):
        if not self._theme_replaying:
            self._theme_entries.clear()
        return super().clear()

    def retheme(self):
        scroll = self.scroll_offset
        auto = self.auto_scroll
        self._theme_replaying = True
        try:
            self.clear()
            for content, args, kwargs in self._theme_entries:
                self.write(content, *args, **{**kwargs, 'scroll_end': False})
            self.scroll_to(scroll.x, scroll.y, animate=False, immediate=True)
            self.auto_scroll = auto
        finally:
            self._theme_replaying = False
